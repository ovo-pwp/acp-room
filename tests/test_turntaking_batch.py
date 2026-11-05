"""core/turntaking.py 的"同批并行"部分 —— TurnBatch / parse_batch / next_step。

P3 只放宽了一件事：**一句话同时 @ 到好几个人**时，这几个人并行跑。
他们看的上文是同一份，互相之间没有依赖，串行等于把两段不相干的等待叠成两倍。
其余情况（一个人接另一个人的话）仍然一步一人、严格串行 ——
那才是"接着说"的前提，改成并行就退回"四个人轮流总结同一个话题"。
"""

import pytest

from core.turntaking import TurnBatch, next_step, parse_batch

R = [("pi", "Pi"), ("omp", "OMP"), ("claude", "Claude Code"), ("codex", "Codex")]


# ---------------------------------------------------------------- parse_batch

@pytest.mark.parametrize("text,speaker,expect", [
    ("@pi @omp 你们俩怎么看", "claude", ["pi", "omp"]),
    ("@omp @pi 顺序不重要", "claude", ["omp", "pi"]),
    ("@pi @pi 说两遍", "codex", ["pi"]),                 # 去重
    ("@pi @omp 我补一句", "pi", ["omp"]),                 # 排除自己
    ("@c @pi 名字有歧义", "codex", ["pi"]),               # @c 谁都像 → 不猜
    ("@all @pi 也叫所有人", "codex", ["pi"]),             # @all 不是某个人
    ("没有 @ 的一句话", "pi", []),
    ("", "pi", []),
    (None, "pi", []),
])
def test_parse_batch(text, speaker, expect):
    assert parse_batch(text, R, speaker=speaker) == expect


def test_parse_batch_只认句首称呼以外的点名():
    """`parse_addressee` 会退到"首句里提到谁"，这里不会。
    首句提到两个人通常是举例，不是同时叫两个人干活。"""
    assert parse_batch("Codex 那条判据的问题出在分母", "pi") == []


# ---------------------------------------------------------------- next_step

def test_同批被点名时并成一步():
    b = next_step(last_text="@pi @omp 你们怎么看", last_speaker="claude",
                  roster=R, spoke={"pi": 1, "omp": 1, "claude": 1},
                  attempts={"pi": 0, "omp": 0, "claude": 1})
    assert isinstance(b, TurnBatch)
    assert b.speakers == ["pi", "omp"]
    assert b.reason == "batch"
    assert "并行" in b.detail


def test_只点到一个人时仍然一步一个():
    b = next_step(last_text="@pi 你怎么看", last_speaker="claude",
                  roster=R, spoke={"claude": 1}, attempts={})
    assert b.speakers == ["pi"]
    assert b.reason == "addressed"


def test_接话不因为提到两个人就变并行():
    """正文里出现两个名字但没有 @：那是"接话"语义，必须串行。"""
    b = next_step(last_text="Pi 说的对，OMP 你也说说", last_speaker="claude",
                  roster=R, spoke={"claude": 1}, attempts={})
    assert b.speakers == ["pi"]              # 首句称呼 → 一个人
    assert b.reason == "addressed"


def test_批次里额度用光的人被剔除():
    b = next_step(last_text="@pi @omp 你们怎么看", last_speaker="claude",
                  roster=R, spoke={"pi": 3, "omp": 1},
                  attempts={"pi": 3, "omp": 1}, max_per_agent=3)
    # pi 到上限 → next_speaker 那边就过不了，改走"还没开过场"的补位；
    # 补位一步一个人，不会成批。
    assert b.reason != "batch"
    assert len(b.speakers) == 1


def test_批次里只剩一个人时不成批():
    """@claude @omp，但 omp 轮到过却没说出内容（tried）——
    批里只剩一个，就该走单人接话那一步，而不是假装两个人在并行。"""
    b = next_step(last_text="@claude @omp 你们怎么看", last_speaker="pi",
                  roster=R, spoke={"pi": 1, "omp": 1},
                  attempts={"pi": 1, "omp": 1}, tried={"omp": 1})
    assert b.reason == "addressed"
    assert b.speakers == ["claude"]


def test_调用方排的队优先于点名():
    b = next_step(last_text="@pi @omp 你们怎么看", last_speaker="claude",
                  roster=R, spoke={}, attempts={}, pending=["codex"])
    assert b.speakers == ["codex"]
    assert b.reason == "addressed"


def test_没人说话时结束():
    spoke = {a: 1 for a, _ in R}
    attempts = {a: 1 for a, _ in R}
    assert next_step(last_text="随便一句谁都没点", last_speaker="pi",
                     roster=R, spoke=spoke, attempts=attempts) is None


def test_as_dict_给审计用():
    assert TurnBatch(["pi", "omp"], "batch", "并行跑").as_dict() == {
        "speakers": ["pi", "omp"], "reason": "batch", "detail": "并行跑"}
