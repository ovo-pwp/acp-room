"""turntaking 的补充测试 —— 名字识别与话题走平。

这两条都是实测踩出来的：
  1. 句首词表太窄，「Codex 那条判据的问题出在分母」匹配不上 → 顺序退化成轮询
  2. 补位规则保证"最少说话的人"总有额度 → 循环走满 budget，也是轮询
"""

import pytest

from core.turntaking import parse_addressee, went_flat, next_speaker

R = [("pi", "Pi"), ("omp", "OMP"), ("claude", "Claude Code"), ("codex", "Codex")]


# ---------------------------------------------------------------- 实测原句

@pytest.mark.parametrize("text,speaker,expect", [
    # 实测里 claude 反驳 codex 的原句。旧词表（这轮|刚才|说得|的）匹配不上它，
    # 于是顺序退化成 pi→omp→claude→codex 轮询。
    ("Codex 那条判据的问题出在分母", "claude", "codex"),
    ("Codex 这轮方向对了", "claude", "codex"),
    ("Pi 说得对", "codex", "pi"),
    ("Claude Code 的判据读反了", "omp", "claude"),
    # 回应但没点名 → 不该被当成在跟某人说话
    ("分母那条接受，3 个模块算均值确实没意义", "codex", None),
    ("那条耦合判据读反了。20 次提交里有 5 次", "claude", None),
    ("同意，补一条可判定的标准", "codex", None),
    # 自称不算：必须由 pi 自己说才是自称
    ("我 Pi 认为这样", "pi", None),
    ("我 Pi 认为这样", "omp", "pi"),   # 别人这么说就是在提 Pi
])
def test_实测句子(text, speaker, expect):
    assert parse_addressee(text, R, speaker=speaker) == expect


# ---------------------------------------------------------------- 词边界

@pytest.mark.parametrize("text", [
    "pipeline 里的 pivot 很慢",       # Pi 藏在 pipeline 里
    "这是 pipeline 的问题",
    "Pipenv 不好用",
    "ClaudeCodexolite 这个词",
    "spider 和 spider 的区别",
])
def test_英文词里不误判(text):
    """agent 名字短（Pi 只有两个字母），不加词边界会在英文里到处误判。"""
    assert parse_addressee(text, R, speaker="omp") is None


def test_只看首句():
    """段中提别人名字通常是举例，不是对话对象。"""
    text = "先说结论。这里要参考 Codex 的看法，但不是现在。"
    assert parse_addressee(text, R, speaker="omp") is None


def test_首句里的名字算数():
    assert parse_addressee("Codex，你怎么看？后面还有别的。", R, speaker="omp") == "codex"


def test_先出现的名字优先():
    """一句话里提了两个人，先提的是对话对象。"""
    assert parse_addressee("OMP 刚才那句我不同意，Codex 也是", R, speaker="pi") == "omp"


# ---------------------------------------------------------------- 话题走平

def test_连续一整轮无人接话就收():
    """实测：不设这条，4 人 rounds=3 会走满 12 步，那是换了顺序的轮询。"""
    assert went_flat(["activity"] * 4, R) is True


def test_一轮未满不收():
    assert went_flat(["activity"] * 3, R) is False


def test_有人接话就重新计数():
    assert went_flat(["addressed", "activity", "activity", "activity"], R) is False
    assert went_flat(["activity", "activity", "addressed", "activity"], R) is False


def test_刚接完话不算走平():
    assert went_flat(["addressed", "addressed", "addressed", "addressed"], R) is False


def test_空历史不收():
    assert went_flat([], R) is False


def test_两人房走平更快():
    assert went_flat(["activity", "activity"], [("a", "A"), ("b", "B")]) is True


def test_补位reason改叫opening():
    """补位的语义变了：不再是"最少说话的人"，而是"还没开过场的人"。

    名字从 activity 改成 opening，是为了让审计里一眼能分清
    "有人接话"（addressed）和"只是轮到它开场"（opening）。
    """
    step = next_speaker(last_text="无", last_speaker="pi", roster=R,
                        spoke={"pi": 3, "omp": 0, "claude": 0, "codex": 0})
    assert step is not None
    assert step.reason == "opening"
    assert step.speaker in ("omp", "claude", "codex")
