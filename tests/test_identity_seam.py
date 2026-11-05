"""身份注入的接缝测试 —— room 是通过 ctx.assemble 把身份发出去的。

这一层要测的是"room 传的东西到底有没有落到 prompt 里"，
而不是 persona 的拼装细节（那些在 test_persona.py）。

真实事故：被点名的 omp 收到 `@omp` 却回了 wait「这条点名 @omp，不是我」。
原因是身份块当时只有名字、且 `assemble` 的调用方没传角色。
"""

import pytest

from core import context as ctx
from core.persona import DEFAULT_TAGS


def _plan():
    return ctx.build_body([], "")


ROSTER = [("pi", "Pi", DEFAULT_TAGS["pi"]), ("omp", "OMP", DEFAULT_TAGS["omp"])]
ROLES = {"pi": ("Pi 的角色文本", DEFAULT_TAGS["pi"]),
         "omp": ("OMP 的角色文本", DEFAULT_TAGS["omp"])}


def test_传了self_id就有身份块():
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES)
    assert "你是谁" in out
    assert "`omp`" in out


def test_不传self_id就不注入身份():
    """向后兼容：老调用方不传身份时行为不变。"""
    out = ctx.assemble("RULES", "", _plan())
    assert "你是谁" not in out
    assert out.startswith("RULES")


def test_身份块在房间规则之后():
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES)
    assert out.index("RULES") < out.index("你是谁")


def test_自己的角色文本进prompt():
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES, self_role="我负责质疑前提")
    assert "我负责质疑前提" in out


def test_别人的角色不进我的prompt():
    """别人的第二人称角色文本写给我看会读着别扭：那是"你是谁"的句子。"""
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES)
    assert "Pi 的角色文本" not in out


def test_名册带别人的tag():
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES)
    assert DEFAULT_TAGS["pi"] in out


def test_被点名时prompt里有硬要求():
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES, addressed=True)
    assert "本轮" in out
    seg = out.split("本轮")[1]
    assert "必须" in seg


def test_没被点名时没有硬要求():
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=ROSTER, roles=ROLES, addressed=False)
    assert "[本轮]" not in out


def test_兼容二元组roster():
    """另一个交付批次用的是 [(id, name)]。改形状会让它静默丢掉角色。"""
    out = ctx.assemble("RULES", "", _plan(), self_name="OMP", self_id="omp",
                       roster=[("pi", "Pi"), ("omp", "OMP")])
    assert "你是谁" in out
    assert "Pi" in out
    # 没有 roles 时，tag 应从内置表补上
    assert DEFAULT_TAGS["pi"] in out


def test_wait提示仍在最后():
    out = ctx.assemble("RULES", "", _plan(), wait_hint="\n\n[WAIT提示]",
                       self_name="OMP", self_id="omp", roster=ROSTER, roles=ROLES)
    assert out.rstrip().endswith("[WAIT提示]")


def test_身份不影响原有正文顺序(turn_factory):
    plan = ctx.build_body([turn_factory(1, "pi", "别人的发言")], "")
    out = ctx.assemble("RULES", "SKILLS", plan, note="NOTE", wait_hint="WAIT",
                       self_name="OMP", self_id="omp", roster=ROSTER, roles=ROLES)
    assert (out.index("RULES") < out.index("你是谁") < out.index("SKILLS")
            < out.index("别人的发言") < out.index("NOTE") < out.index("WAIT"))
