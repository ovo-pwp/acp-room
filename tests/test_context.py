"""core/context.py 的单测 —— 上下文组装。

这些测试**不启动任何 ACP 进程、不读 config.json、不碰 Room**。
如果哪天跑不过了，说明有人把宿主依赖又塞回 core 了。
"""

import pytest

from core import context as ctx


# ---------------------------------------------------------------- pending_since

def test_排除自己的发言(turn_factory):
    ts = [turn_factory(1, "pi", "我说"), turn_factory(2, "omp", "他说"),
          turn_factory(3, "pi", "我又说")]
    got = ctx.pending_since(ts, 0, "pi")
    assert [t.speaker for t in got] == ["omp"]


def test_游标之后才算(turn_factory):
    ts = [turn_factory(i, "a" if i % 2 else "b", f"m{i}") for i in range(1, 6)]
    got = ctx.pending_since(ts, 3, "zzz")
    assert [t.seq for t in got] == [4, 5]


def test_游标为0取全部(turn_factory):
    ts = [turn_factory(1, "a", "x"), turn_factory(2, "b", "y")]
    assert len(ctx.pending_since(ts, 0, "zzz")) == 2


# ---------------------------------------------------------------- build_body

def test_没有发言也没有附言(turn_factory):
    plan = ctx.build_body([], "")
    assert plan.total == 0
    assert "上一轮没有人说话" in plan.body


def test_有附言但没发言(turn_factory):
    plan = ctx.build_body([], "主持人说两句")
    assert "见下面的主持人附言" in plan.body


def test_少于一屏时全部全文(turn_factory):
    ts = [turn_factory(i, f"a{i}", "短" * 10) for i in range(3)]
    plan = ctx.build_body(ts, "")
    assert plan.compressed == 0
    assert "已压缩" not in plan.body
    assert plan.body.count("[A") == 3


def test_超过near就压缩更早的(turn_factory):
    # 10 条 → 前 4 条压缩，后 6 条全文
    ts = [turn_factory(i, f"a{i}", "很长的内容" * 50) for i in range(10)]
    plan = ctx.build_body(ts, "")
    assert plan.total == 10
    assert plan.compressed == 4
    assert "（以下 4 条较早发言已压缩）" in plan.body


def test_压缩后带标记(turn_factory):
    long_text = "结论" * 200          # 400 字
    ts = [turn_factory(i, "a", long_text) for i in range(8)]
    plan = ctx.build_body(ts, "")
    assert "…（已压缩）" in plan.body
    # 压缩后的单条不能超过 COMPRESS + 标记长度
    for line in plan.body.split("\n\n"):
        if "已压缩" in line and line.startswith("[A"):
            body = line.split("\n", 1)[1]
            assert len(body) <= ctx.COMPRESS + len("…（已压缩）")


def test_单条超长被硬截断(turn_factory):
    huge = "x" * (ctx.HARD_CAP + 500)
    ts = [turn_factory(i, "a", huge) for i in range(3)]
    plan = ctx.build_body(ts, "")
    assert plan.truncated == 3
    assert "…（截断）" in plan.body


def test_空正文的发言被跳过(turn_factory):
    ts = [turn_factory(1, "a", ""), turn_factory(2, "b", "   "), turn_factory(3, "c", "有内容")]
    plan = ctx.build_body(ts, "")
    assert plan.body.strip().endswith("有内容")
    assert plan.body.count("[A]\n") == 0


def test_边界值near等于条数(turn_factory):
    ts = [turn_factory(i, "a", "字" * 300) for i in range(ctx.NEAR)]
    plan = ctx.build_body(ts, "")
    assert plan.compressed == 0


def test_边界值near加一(turn_factory):
    ts = [turn_factory(i, "a", "字" * 300) for i in range(ctx.NEAR + 1)]
    plan = ctx.build_body(ts, "")
    assert plan.compressed == 1


# ---------------------------------------------------------------- image_note

def test_没有图片返回空串():
    assert ctx.image_note([]) == ""


def test_图片说明只列前五个名字():
    atts = [{"id": f"id{i}", "name": f"p{i}.png"} for i in range(7)]
    note = ctx.image_note(atts)
    assert "7 张图片" in note
    assert "p0.png" in note
    assert "p6.png" not in note
    assert "等 7 张" in note


def test_图片没有名字时用id():
    note = ctx.image_note([{"id": "abc123"}])
    assert "abc123" in note


# ---------------------------------------------------------------- assemble

def test_拼装顺序固定(turn_factory):
    plan = ctx.build_body([turn_factory(1, "a", "内容")], "")
    out = ctx.assemble("PREAMBLE", "SKILLS", plan, note="NOTE", wait_hint="WAIT")
    i_p = out.index("PREAMBLE")
    i_s = out.index("SKILLS")
    i_b = out.index("内容")
    i_n = out.index("NOTE")
    i_w = out.index("WAIT")
    assert i_p < i_s < i_b < i_n < i_w


def test_没有附言就不出主持人段(turn_factory):
    plan = ctx.build_body([turn_factory(1, "a", "x")], "")
    out = ctx.assemble("P", "", plan, note="")
    assert "[主持人]" not in out


def test_技能块为空不影响正文(turn_factory):
    plan = ctx.build_body([turn_factory(1, "a", "正文在此")], "")
    out = ctx.assemble("P", "", plan)
    assert "正文在此" in out


# ---------------------------------------------------------------- 其它小工具

def test_pick_topic取最近的人类发言(turn_factory):
    ts = [turn_factory(1, "human", "早先的问题"), turn_factory(2, "pi", "回答"),
          turn_factory(3, "human", "最新的问题")]
    assert ctx.pick_topic(ts) == "最新的问题"


def test_pick_topic跳过空人类发言(turn_factory):
    ts = [turn_factory(1, "human", "有用的"), turn_factory(2, "human", "   ")]
    assert ctx.pick_topic(ts) == "有用的"


def test_pick_topic没有人类发言返回空串(turn_factory):
    assert ctx.pick_topic([turn_factory(1, "pi", "x")]) == ""


def test_activity只统计窗口内(turn_factory):
    ts = [turn_factory(i, "pi" if i < 5 else "omp", "x") for i in range(10)]
    a = ctx.activity(ts, ["pi", "omp"], window=30)
    assert a == {"pi": 5, "omp": 5}


def test_activity窗口截断(turn_factory):
    ts = [turn_factory(i, "pi" if i < 5 else "omp", "x") for i in range(10)]
    a = ctx.activity(ts, ["pi", "omp"], window=3)
    assert a == {"pi": 0, "omp": 3}


def test_activity忽略不认识的人(turn_factory):
    ts = [turn_factory(1, "ghost", "x"), turn_factory(2, "pi", "y")]
    assert ctx.activity(ts, ["pi"]) == {"pi": 1}


def test_last_human_text(turn_factory):
    ts = [turn_factory(1, "human", "第一"), turn_factory(2, "pi", "er"), turn_factory(3, "human", "第二")]
    assert ctx.last_human_text(ts) == "第二"


def test_last_human_text没有就空串(turn_factory):
    assert ctx.last_human_text([turn_factory(1, "pi", "x")]) == ""


# ---------------------------------------------------------------- wait 判定

import re

WAIT_RE = re.compile(r"^\s*\**\s*\[WAIT\]\s*\**\s*", re.I)


@pytest.mark.parametrize("text,expect", [
    ("[WAIT] 没话说", "没话说"),
    ("[wait] 小写也认", "小写也认"),
    ("  [WAIT]  前后空白", "前后空白"),
    ("**[WAIT]** 带 markdown", "带 markdown"),
    ("[WAIT]", "（未说明理由）"),
    ("我同意 [WAIT] 但这不是开头", None),
    ("普通发言", None),
    ("", None),
])
def test_wait_reason(text, expect):
    got = ctx.wait_reason(text, WAIT_RE)
    if expect is None:
        assert got is None
    else:
        assert got == expect


def test_wait理由被截到80字():
    got = ctx.wait_reason("[WAIT] " + "字" * 200, WAIT_RE)
    assert len(got) == 80
