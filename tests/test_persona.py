"""core/persona.py 的单测 —— 身份与人格。

这块的 bug 症状是"模型偶尔不回复"，肉眼查不出来，所以必须逐条钉住。
真实事故：被点名的 omp 回「这条点名 @omp，不是我」，静默吃掉一整轮发言。
"""

import pytest

from core import persona
from core.persona import DEFAULT_ROLES, DEFAULT_TAGS, FALLBACK_ROLE, Persona


def P(aid, name=None, **kw):
    return Persona(id=aid, name=name or aid.title(), **kw)


ROOM = [P("pi", "Pi"), P("omp", "OMP"), P("claude", "Claude Code"), P("codex", "Codex")]


# ---------------------------------------------------------------- 从配置构建

def test_有role时用配置的role():
    p = Persona.from_cfg({"id": "x", "name": "X", "role": "我是自定义角色"})
    assert p.role == "我是自定义角色"


def test_缺role时用内置默认():
    p = Persona.from_cfg({"id": "pi", "name": "Pi"})
    assert p.role == DEFAULT_ROLES["pi"]
    assert p.role  # 非空


def test_未知id用兜底role():
    p = Persona.from_cfg({"id": "brand-new", "name": "New"})
    assert p.role == FALLBACK_ROLE


def test_缺name时用id():
    assert Persona.from_cfg({"id": "pi"}).name == "pi"


def test_四个内置角色互不相同():
    """人格雷同的圆桌会变成四个复读机。"""
    roles = [DEFAULT_ROLES[k] for k in ("pi", "omp", "claude", "codex")]
    assert len(set(roles)) == 4
    tags = [DEFAULT_TAGS[k] for k in ("pi", "omp", "claude", "codex")]
    assert len(set(tags)) == 4


# ---------------------------------------------------------------- 身份块

def test_身份块写明自己的id和名字():
    out = persona.identity_block(P("omp", "OMP"))
    assert "OMP" in out
    assert "`omp`" in out


def test_身份块带角色():
    me = P("omp", "OMP", role="你负责质疑前提")
    assert "你负责质疑前提" in persona.identity_block(me)


def test_身份块带说话方式():
    me = P("omp", role="角色", style="直接，不客套")
    assert "直接，不客套" in persona.identity_block(me)


def test_身份块不为自己编角色():
    """没给 role 也不该输出空的「说话方式：」之类残句。"""
    out = persona.identity_block(P("x", "X", role="", style=""))
    assert "说话方式" not in out
    assert out.count("\n") <= 2


# ---------------------------------------------------------------- 名册

def test_名册排除自己():
    """把自己列进"房间里其他人"会让模型以为有两个自己。"""
    out = persona.roster_block(P("omp", "OMP"), ROOM)
    assert "Pi" in out and "Codex" in out
    # OMP 只应作为"我"出现，不出现在其他人的列表里
    body = out.split("\n", 1)[1]
    assert "OMP" not in body


def test_名册用tag而不是角色原文():
    """名册是"别人是谁"，塞第二人称角色原文既别扭又浪费上下文。"""
    out = persona.roster_block(P("omp", "OMP"), ROOM)
    assert DEFAULT_TAGS["pi"] in out
    assert DEFAULT_ROLES["pi"] not in out


def test_名册为空时明说():
    out = persona.roster_block(P("omp", "OMP"), [P("omp", "OMP")])
    assert "只有你一个" in out


def test_名册带id方便点名():
    out = persona.roster_block(P("omp", "OMP"), ROOM)
    assert "`pi`" in out
    assert "`claude`" in out


# ---------------------------------------------------------------- 称呼规则

def test_称呼规则说清at自己等于叫自己():
    out = persona.addressing_block(P("omp", "OMP"), ROOM)
    assert "@omp" in out
    assert "@OMP" in out
    assert "必须回答" in out


def test_称呼规则列出别人():
    out = persona.addressing_block(P("omp", "OMP"), ROOM)
    assert "@pi" in out and "@claude" in out and "@codex" in out
    assert "@omp" not in out.split("不是在叫你")[1] if "不是在叫你" in out else True


def test_称呼规则说明别人发言里的at与我无关():
    """玩家经常在正文里写 @，模型会误以为那也是点名。"""
    out = persona.addressing_block(P("omp", "OMP"), ROOM)
    assert "别人发言" in out


# ---------------------------------------------------------------- 完整拼装

def test_build三段齐全且顺序固定():
    out = persona.build(P("omp", "OMP"), ROOM)
    i_me = out.index("[你是谁]")
    i_room = out.index("[房间里]")
    i_at = out.index("[怎么称呼]")
    assert i_me < i_room < i_at


def test_被点名时追加硬要求():
    out = persona.build(P("omp", "OMP"), ROOM, addressed=True)
    assert "[本轮]" in out
    assert "必须" in out.split("[本轮]")[1]


def test_没被点名时不加本轮段():
    out = persona.build(P("omp", "OMP"), ROOM, addressed=False)
    assert "[本轮]" not in out


def test_身份块长度可控():
    """每轮都发，太长会白烧 token。"""
    out = persona.build(P("omp", "OMP"), ROOM)
    assert len(out) < 1200, f"身份块 {len(out)} 字符，太长了"


# ---------------------------------------------------------------- from_roster（兼容旧形状）

def test_from_roster兼容二元组():
    """另一个交付批次用的是 [(id, name)]，改形状会让它静默失效。"""
    me, others = persona.from_roster("omp", "OMP", [("pi", "Pi"), ("omp", "OMP")])
    assert me.id == "omp"
    assert [o.id for o in others] == ["pi", "omp"]
    assert others[0].tag == DEFAULT_TAGS["pi"]      # 从内置表补上


def test_from_roster接受三元组tag():
    me, others = persona.from_roster("omp", "OMP", [("pi", "Pi", "我的自定义标签")])
    assert others[0].tag == "我的自定义标签"


def test_from_roster接受roles映射():
    me, others = persona.from_roster(
        "omp", "OMP", [("pi", "Pi")], roles={"pi": ("自定义角色", "自定义标签")})
    assert others[0].role == "自定义角色"
    assert others[0].tag == "自定义标签"


def test_from_roster自己的角色走self_role():
    me, _ = persona.from_roster("omp", "OMP", [], self_role="我的角色", self_tag="我的标签")
    assert me.role == "我的角色"
    assert me.tag == "我的标签"


def test_from_roster忽略残缺条目():
    me, others = persona.from_roster("omp", "OMP", [("pi",), ("codex", "Codex")])
    assert [o.id for o in others] == ["codex"]


# ---------------------------------------------------------------- brief

def test_brief优先用tag():
    assert P("x", tag="短标签", role="很长的角色文本" * 20).brief() == "短标签"


def test_brief无tag时截断():
    b = P("x", role="很长的角色文本" * 20).brief()
    assert len(b) <= 61      # 60 + 省略号
    assert b.endswith("…")
