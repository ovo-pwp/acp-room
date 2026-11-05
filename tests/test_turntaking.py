"""core/turntaking.py 的单测 —— 自由发言的顺序决定。

这块决定了"谁接下句"，规则写错的表现是"对话卡住"或"两个人无限互相点名"，
都不是会报错的失败，所以每条分支都要钉住。
"""

import pytest

from core.turntaking import (
    Step,
    budget_steps,
    match_name,
    next_speaker,
    opening,
    parse_addressee,
)

R = [("pi", "Pi"), ("omp", "OMP"), ("claude", "Claude Code"), ("codex", "Codex")]


# ---------------------------------------------------------------- match_name

@pytest.mark.parametrize("tok,expect", [
    ("pi", "pi"),                    # 精确 id
    ("PI", "pi"),                    # 大小写不敏感
    ("Claude Code", "claude"),       # 精确名字
    ("cl", "claude"),                # 唯一前缀（只有 claude 以 cl 开头）
    ("c", None),                     # 歧义：codex 和 claude 都能匹配 → 不猜
    ("nobody", None),
    ("", None),
    ("all", None),                   # @all 是"所有人"，不是某人
])
def test_match_name(tok, expect):
    assert match_name(tok, R) == expect


# ---------------------------------------------------------------- parse_addressee

@pytest.mark.parametrize("text,speaker,expect", [
    ("@codex 这轮你错了", "pi", "codex"),
    ("@pi 你自己说说", "omp", "pi"),
    ("Codex 这轮方向对了", "claude", "codex"),        # 句首称呼
    ("Pi 说得对", "codex", "pi"),
    ("我 Pi 认为这样", "pi", None),                    # 自称不算点名
    ("@pi 我觉得", "pi", None),                        # @自己不算点名
    ("普通的一句话，没有 @", "omp", None),
    ("", "pi", None),
    ("@cl 你看呢", "codex", "claude"),
])
def test_parse_addressee(text, speaker, expect):
    assert parse_addressee(text, R, speaker=speaker) == expect


def test_at优先于句首称呼():
    """两者都命中时以 @ 为准：它更明确。"""
    assert parse_addressee("@omp Codex 这轮说得对", R, speaker="pi") == "omp"


# ---------------------------------------------------------------- next_speaker

def test_被点名的人接话():
    step = next_speaker(last_text="@codex 你说", last_speaker="pi",
                        roster=R, spoke={"pi": 1})
    assert step.speaker == "codex"
    assert step.reason == "addressed"


def test_补位只给还没开过场的人():
    """补位现在只负责"让每个人都有一次立场陈述"。"""
    step = next_speaker(last_text="就这样吧", last_speaker="pi", roster=R,
                        spoke={"pi": 2, "omp": 2, "claude": 0, "codex": 1})
    assert step.speaker == "claude"
    assert step.reason == "opening"


def test_都开过场且没人接话就结束():
    """**这条锁住一个实测反例。**

    原来补位是"选说话最少的人"，那本身就是轮询 —— 实测日志
    `pi omp claude codex pi omp claude codex …` 连续 12 步 reason 全是 activity，
    顺序和名册一模一样。改成"只给没开过场的人"之后，全员说过就返回 None，
    对话只能靠"被点名/接话"延续。
    """
    spoke = {"pi": 1, "omp": 1, "claude": 1, "codex": 1}
    assert next_speaker(last_text="没有点名", last_speaker="claude", roster=R,
                        spoke=spoke) is None


def test_补位不选刚说完的人():
    """还没开过场的人里也不会是刚说完的那个。"""
    step = next_speaker(last_text="没有点名", last_speaker="pi", roster=R,
                        spoke={"pi": 1, "omp": 0, "claude": 0, "codex": 0})
    assert step.speaker != "pi"
    assert step.reason == "opening"


def test_wait过的人仍算没开过场():
    """回了 wait 说明它没说话，下次仍该给它机会；但 attempts 已涨，不会无限选它。"""
    step = next_speaker(last_text="无", last_speaker="pi", roster=R,
                        spoke={"pi": 1, "omp": 0, "claude": 1, "codex": 1},
                        attempts={"pi": 1, "omp": 1, "claude": 1, "codex": 1})
    assert step.speaker == "omp"
    assert step.reason == "opening"


def test_并列最少时按名册顺序保证可复现():
    step = next_speaker(last_text="无", last_speaker="codex", roster=R,
                        spoke={"pi": 0, "omp": 0, "claude": 0, "codex": 0})
    assert step.speaker == "pi"


def test_被点名者到上限时换人并说明():
    step = next_speaker(last_text="@codex 你说", last_speaker="pi", roster=R,
                        spoke={"codex": 3}, max_per_agent=3)
    assert step.speaker != "codex"
    assert "上限" in step.detail


def test_全部到上限返回None():
    step = next_speaker(last_text="x", last_speaker="pi", roster=R,
                        spoke={a: 3 for a, _ in R}, max_per_agent=3)
    assert step is None


def test_只剩自己一人有空时不无限自言自语():
    """只有一个 agent 且它已说过 → 结束，不要让它自己跟自己聊。"""
    step = next_speaker(last_text="x", last_speaker="pi", roster=[("pi", "Pi")],
                        spoke={"pi": 1}, max_per_agent=3)
    assert step is None


def test_空名册返回None():
    assert next_speaker(last_text="x", last_speaker="pi", roster=[], spoke={}) is None


def test_pending优先于推断():
    """人类已经点过名了，不要替他改。"""
    step = next_speaker(last_text="@codex 你说", last_speaker="pi", roster=R,
                        spoke={}, pending=["omp"])
    assert step.speaker == "omp"
    assert "排在队首" in step.detail


def test_pending里的人到上限则忽略():
    step = next_speaker(last_text="无", last_speaker="pi", roster=R,
                        spoke={"omp": 9}, pending=["omp"], max_per_agent=3)
    assert step.speaker != "omp"


# ---------------------------------------------------------------- opening

def test_开场优先给点名的人():
    step = opening(roster=R, activity={}, addressed=["omp"])
    assert step.speaker == "omp"
    assert step.reason == "addressed"


def test_没人点名时最近说得最少的开场():
    step = opening(roster=R, activity={"pi": 5, "omp": 3, "claude": 1, "codex": 0})
    assert step.speaker == "codex"
    assert step.reason == "first"


def test_空名册开场返回None():
    assert opening(roster=[], activity={}) is None


# ---------------------------------------------------------------- budget_steps

def test_步数上限随人数和轮数增长():
    assert budget_steps(4, 1) < budget_steps(4, 2) < budget_steps(4, 3)


def test_步数上限有绝对封顶():
    """链式接话必须能被截断，否则 A 问 B、B 反问 A 会绕圈。"""
    assert budget_steps(4, 99) <= 4 * 3 * 2


def test_人数为0不炸():
    assert budget_steps(0, 1) >= 1      # max(1, n) 兜底，不该返回 0 或负数


# ---------- tried：轮到过但没说出内容 ----------


def test_空正文的人不该反复占补位名额():
    """故障 agent 不能靠"没说话"永远留在补位候选里。

    实测形状：pi 因网关 401 连续两次空正文，日志是

        turn_pick pi | opening: 还没开过场，轮到 pi
        turn_empty pi
        turn_pick pi | opening: 还没开过场，轮到 pi
        turn_empty pi

    补位的本意是"让还没轮到的人有机会"，不是"让说空话的人说到说出来为止"。
    """
    roster = [("pi", "Pi"), ("omp", "Omp")]
    step = next_speaker(
        last_text="", last_speaker="pi", roster=roster,
        spoke={"pi": 0, "omp": 0}, attempts=None,
        max_per_agent=3, tried={"pi": 1, "omp": 0})
    assert step is not None and step.speaker == "omp"


def test_没人空过场时补位行为不变():
    """tried 缺省为空 → 行为和加这个参数之前完全一样。"""
    roster = [("pi", "Pi"), ("omp", "Omp")]
    step = next_speaker(
        last_text="", last_speaker="pi", roster=roster,
        spoke={"pi": 0, "omp": 0}, attempts=None, max_per_agent=3)
    assert step is not None and step.reason == "opening"


def test_全都空过场就结束而不是硬撑():
    """没有新人可补 → 话题该收，而不是把预算耗在故障 agent 上。"""
    roster = [("pi", "Pi"), ("omp", "Omp")]
    step = next_speaker(
        last_text="", last_speaker="pi", roster=roster,
        spoke={"pi": 0, "omp": 0}, attempts=None,
        max_per_agent=3, tried={"pi": 1, "omp": 1})
    assert step is None


def test_轮询计数与空正文计数互不干扰():
    """spoke 和 tried 是两个维度：说过话的人不再补位，空过场的人也不补位。"""
    roster = [("pi", "Pi"), ("omp", "Omp"), ("x", "X")]
    step = next_speaker(
        last_text="", last_speaker="pi", roster=roster,
        spoke={"pi": 5, "omp": 0, "x": 0}, attempts=None,
        max_per_agent=9, tried={"pi": 0, "omp": 2, "x": 0})
    assert step is not None and step.speaker == "x"

