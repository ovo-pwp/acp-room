"""core/convergence.py 的单测 —— 收敛判据。

这是本项目里最值得单测的一块：判据决定了讨论什么时候停，
而它原来的验证方式是"跑一场 4 分钟的真实讨论然后肉眼看"。
现在每条分支都能用几行 fixture 精确验证。

**不启动任何 ACP 进程、不读 config.json、不构造 Room。**
"""

import re

import pytest

from core.convergence import (
    WATCHDOG_CAP,
    Budget,
    Convergence,
    TokenMeter,
    phases_for,
    phases_that_accept_marker,
    pick_least_similar,
    signature,
    similar,
    similarity_value,
    standard_phase_count,
    watchdog_seconds,
)


# ---------------------------------------------------------------- 阶段协议

def test_三种讨论法都有阶段():
    for m in ("standard", "advocate", "oxford"):
        assert len(phases_for(m)) >= 3


def test_未知方法回退standard():
    """方法名是配置项，写错不该让房间停摆。"""
    assert phases_for("nope") == phases_for("standard")


def test_标准五阶段计数():
    # standard 表里只有 4 个阶段，汇总（synthesis）是第 5 个，单独一步
    assert standard_phase_count("standard") == 5
    assert standard_phase_count("oxford") == 4


@pytest.mark.parametrize("phase,ok", [
    ("answer", False),      # 信息最少，最容易抢答，不认
    ("critique", False),
    ("discuss", True),
    ("position", True),
    ("challenge", True),
    ("closing", True),
])
def test_哪些阶段认终局标记(phase, ok):
    assert (phase in phases_that_accept_marker()) is ok


# ---------------------------------------------------------------- 指纹与相似度

def test_指纹中文按二字组(turn_factory):
    """注意 `re.findall` 的匹配**不重叠**：得到 你好/世界，没有 好世。

    这不是缺陷：指纹只用来比较两轮的重合度，非重叠二字组一样能区分
    "还在产新论点"（0.1 段）和"原地复读"（0.7 段）。刻意不引 jieba 分词。
    """
    sig = signature([turn_factory(1, "a", "你好世界")])
    assert sig == {"你好", "世界"}


def test_指纹英文按词且忽略短词(turn_factory):
    sig = signature([turn_factory(1, "a", "the quick brown fox is ok")])
    assert "quick" in sig
    assert "brown" in sig
    assert "the" not in sig      # 3 字母以下不要
    assert "ok" not in sig


def test_指纹小写归一(turn_factory):
    assert signature([turn_factory(1, "a", "Hello World")]) == \
           signature([turn_factory(1, "a", "hello world")])


def test_空文本指纹为空():
    assert signature([]) == set()


@pytest.mark.parametrize("a,b,expect", [
    ({"x", "y"}, {"x", "y"}, 1.0),
    ({"x", "y"}, {"x", "z"}, 1 / 3),
    ({"x"}, {"y"}, 0.0),
    (set(), {"x"}, 0.0),          # 空集返回 0，不是 1
    ({"x"}, set(), 0.0),
    (set(), set(), 0.0),
])
def test_jaccard(a, b, expect):
    assert similarity_value(a, b) == pytest.approx(expect)


def test_similar按阈值():
    assert similar({"a", "b", "c"}, {"a", "b", "c"}, 0.9)
    assert not similar({"a", "b", "c"}, {"x", "y", "z"}, 0.9)


# ---------------------------------------------------------------- token 计费

def test_token取增量不取累计(turn_factory):
    """实测把累计值直接相加会双计（12 条发言报 142k）。"""
    m = TokenMeter()
    t1 = turn_factory(1, "omp", "x", tokens=12852)
    t2 = turn_factory(2, "omp", "y", tokens=13686)
    t3 = turn_factory(3, "omp", "z", tokens=16219)
    assert m.cost(t1) == 12852
    assert m.cost(t2) == 834
    assert m.cost(t3) == 2533


def test_token按发言人分开记(turn_factory):
    m = TokenMeter()
    assert m.cost(turn_factory(1, "omp", "x", tokens=1000)) == 1000
    assert m.cost(turn_factory(2, "claude", "y", tokens=500)) == 500
    assert m.cost(turn_factory(3, "omp", "z", tokens=1200)) == 200


def test_token为0的贡献0(turn_factory):
    """pi 的 agent 恒报 0，这是已知的会计口径缺口。"""
    m = TokenMeter()
    assert m.cost(turn_factory(1, "pi", "x", tokens=0)) == 0
    assert m.cost(turn_factory(2, "pi", "y")) == 0


def test_token回退说明会话重建(turn_factory):
    """累计值变小 = session 被重建，退回全量而不是负数。"""
    m = TokenMeter()
    m.cost(turn_factory(1, "omp", "x", tokens=5000))
    assert m.cost(turn_factory(2, "omp", "y", tokens=300)) == 300


def test_charge累加总账(turn_factory):
    c = Convergence()
    c.charge([turn_factory(1, "omp", "x", tokens=1000)])
    c.charge([turn_factory(2, "omp", "y", tokens=1500)])
    assert c.tokens_used == 1500      # 1000 + 500


# ---------------------------------------------------------------- 判据顺序

def test_空轮不停():
    assert Convergence().judge([]).stop is False


def test_标记生效时命中共识(turn_factory):
    c = Convergence()
    c.markers_live = True
    v = c.judge([turn_factory(1, "pi", "我认这个结论 [CONSENSUS]")])
    assert v.stop and v.kind == "consensus"
    assert "Pi" in v.reason


def test_标记生效时命中死锁(turn_factory):
    c = Convergence()
    c.markers_live = True
    v = c.judge([turn_factory(1, "Codex", "两条路都走不通 [DEADLOCK]", name="Codex")])
    assert v.stop and v.kind == "deadlock"


def test_标记不生效时被忽略(turn_factory):
    """answer / critique 阶段不认标记——防模型抢答收敛。"""
    c = Convergence()
    c.markers_live = False
    assert c.judge([turn_factory(1, "pi", "[CONSENSUS]")]).stop is False


def test_标记大小写不敏感(turn_factory):
    c = Convergence()
    c.markers_live = True
    assert c.judge([turn_factory(1, "pi", "[consensus]")]).kind == "consensus"


def test_token预算耗尽(turn_factory):
    c = Convergence(Budget(max_tokens=100))
    c.tokens_used = 100
    v = c.judge([turn_factory(1, "omp", "x")])
    assert v.stop and v.kind == "token_budget"


def test_超时(turn_factory):
    c = Convergence(Budget(max_seconds=10))
    c.started_at = 1000.0
    v = c.judge([turn_factory(1, "omp", "x")], now=1011.0)
    assert v.stop and v.kind == "timeout"


def test_没经过run就不计时(turn_factory):
    """started_at==0 说明没经过 run()，无从计时，不该被误判超时。"""
    c = Convergence(Budget(max_seconds=1))
    assert c.judge([turn_factory(1, "omp", "x")], now=999999.0).stop is False


# 顺序：标记要压过预算和超时
def test_顺序_标记优先于预算(turn_factory):
    c = Convergence(Budget(max_tokens=10))
    c.tokens_used = 999
    c.markers_live = True
    assert c.judge([turn_factory(1, "pi", "[CONSENSUS]")]).kind == "consensus"


def test_顺序_预算优先于超时(turn_factory):
    c = Convergence(Budget(max_tokens=10, max_seconds=1))
    c.tokens_used = 999
    c.started_at = 1000.0
    assert c.judge([turn_factory(1, "omp", "x")], now=99999.0).kind == "token_budget"


# ---------------------------------------------------------------- 无进展

def test_无进展_连续两轮高度重合(turn_factory):
    c = Convergence(Budget(no_progress_rounds=2, no_progress_threshold=0.45))
    c.collect = True
    text = "同样的论点反复说同样的论点反复说"
    c.judge([turn_factory(1, "a", text)])
    v = c.judge([turn_factory(2, "b", text)])
    assert v.stop and v.kind == "no_progress"


def test_无进展_论点持续更新则不停(turn_factory):
    c = Convergence(Budget(no_progress_rounds=2, no_progress_threshold=0.45))
    c.collect = True
    c.judge([turn_factory(1, "a", "甲甲甲甲 乙乙乙乙")])
    v = c.judge([turn_factory(2, "b", "丙丙丙丙 丁丁丁丁")])
    assert v.stop is False


def test_无进展_只在collect时比对(turn_factory):
    """answer→critique 换阶段文本必然不同，跨阶段比会误判。"""
    c = Convergence(Budget(no_progress_rounds=2))
    c.collect = False
    text = "一模一样的话一模一样的话"
    c.judge([turn_factory(1, "a", text)])
    assert c.judge([turn_factory(2, "b", text)]).stop is False
    assert c.round_signatures == []      # 没 collect 就不留签名


def test_无进展_轮数不够不停(turn_factory):
    c = Convergence(Budget(no_progress_rounds=3))
    c.collect = True
    text = "重复重复重复"
    c.judge([turn_factory(1, "a", text)])
    assert c.judge([turn_factory(2, "b", text)]).stop is False


# ---------------------------------------------------------------- trace / 可观测

def test_trace记录每轮重合度(turn_factory):
    c = Convergence()
    c.collect = True
    c.judge([turn_factory(1, "a", "第一轮内容")])
    c.judge([turn_factory(2, "b", "第一轮内容")])
    assert len(c.trace) == 2
    assert c.trace[0]["jaccard_vs_prev"] == 0.0     # 第一轮没有前值
    assert c.trace[0]["new_terms"] is None
    assert c.trace[1]["jaccard_vs_prev"] == 1.0
    assert c.trace[1]["new_terms"] == 0


def test_summary可序列化(turn_factory):
    c = Convergence()
    c.collect = True
    c.judge([turn_factory(1, "a", "x" * 10, tokens=100)])
    s = c.summary()
    assert s["rounds_measured"] == 1
    assert s["collect"] is True
    import json
    json.dumps(s)      # 必须能进审计 jsonl


# ---------------------------------------------------------------- 兜底选代言人

def test_选最孤立的人当魔鬼代言人():
    """isolation = max(与任何人的重合度)，取最小的 —— 最没人替它说话的立场。"""
    sigs = {
        "pi":     {"a", "b", "c"},
        "omp":    {"a", "b", "c"},
        "codex":  {"x", "y", "z"},     # 和别人完全不像 → 最孤立
    }
    assert pick_least_similar(["pi", "omp", "codex"], sigs) == "codex"


def test_回归_原来的min会让选择退化成选第一个():
    """这条测试锁住一个已修的真 bug。

    原实现用 min(与其它人的重合度)：只要群里存在任何一对不像的成员，
    所有人都是 0.00，min() 只好按顺序取第一个 —— "兜底选人"永远选第一个发言人。
    现在改成 max，两人的位置对调后结果也必须跟着对调。
    """
    two_sides = {"pi": {"a", "b"}, "codex": {"x", "y"}}
    # 两个都不像对方，无从区分 → 按顺序取第一个（这是可接受的并列行为）
    assert pick_least_similar(["pi", "codex"], two_sides) == "pi"

    # 三方时必须有区分度：codex 与另外两个都不像，另两个互相很像
    sigs = {"pi": {"a", "b", "c"}, "omp": {"a", "b", "c"}, "codex": {"x", "y"}}
    assert pick_least_similar(["pi", "omp", "codex"], sigs) == "codex"
    # 顺序换一下，选中的仍然是 codex（说明不是"按顺序取第一个"）
    assert pick_least_similar(["codex", "pi", "omp"], sigs) == "codex"


def test_没立场的人排在最后():
    """签名是空的说明拿不到立场信号，当魔鬼代言人只是随机 —— 优先级最低。"""
    sigs = {"pi": {"a", "b"}, "omp": {"a", "b"}, "quiet": set()}
    assert pick_least_similar(["pi", "omp", "quiet"], sigs) == "pi"
    # 但只有它一个人说话时，还是得用它
    assert pick_least_similar(["quiet"], {"quiet": set()}) == "quiet"


def test_只有一个人有立场时选它():
    assert pick_least_similar(["pi", "silent"], {"pi": {"a"}, "silent": set()}) == "pi"


def test_空候选返回None():
    assert pick_least_similar([], {}) is None


# ---------- 整轮看门狗 ----------


def test_看门狗默认是单轮两倍():
    assert watchdog_seconds(900) == 1800


def test_看门狗绝对封顶半小时():
    """不能是 turn_timeout×步数：8 步 × 900s = 2 小时，等于没有看门狗。

    真卡住了，用户要瞪着转圈两小时才知道。正常一轮两分钟就跑完，
    超过半小时一定是哪里不对。
    """
    assert watchdog_seconds(9000) == WATCHDOG_CAP
    assert WATCHDOG_CAP == 1800


def test_看门狗可显式覆盖():
    assert watchdog_seconds(900, 60) == 60
    assert watchdog_seconds(900, 99999) == 99999


def test_看门狗显式传0视为未设置():
    """0 在 config 里通常表示"没填"，应退回默认而不是立刻超时。"""
    assert watchdog_seconds(900, 0) == 1800


def test_看门狗永远是个正数():
    """再短的 turn_timeout 也不能算出 0 或负数——那会让每一轮立刻超时。"""
    for tt_ in (0, 1, 2, 900, 100000):
        assert watchdog_seconds(tt_) > 0


def test_账本跨讨论不清零否则第一轮按全量计费(turn_factory):
    """`Discussion.run()` 里那句"每场清一次累计值"不能加回来。

    第一次见到某人时没有基线，只能按全量计（上面两条用例钉死了这个口径）。
    所以每场开头清零 = 每场第一句话都把 agent 整个会话的历史 token 算进预算。
    实测：omp 一场讨论第一轮记到 20830，20000 的预算当场判"用尽"停摆。
    """
    m = TokenMeter()
    assert m.cost(turn_factory(1, "omp", "x", tokens=200000)) == 200000
    assert m.cost(turn_factory(2, "omp", "y", tokens=201500)) == 1500

    # 第二场：同一个账本继续用，只增不减
    assert m.cost(turn_factory(3, "omp", "z", tokens=203000)) == 1500
    # 会话被重建（累计值反而变小）时退回全量，这是唯一该按全量计的情形
    assert m.cost(turn_factory(4, "omp", "w", tokens=800)) == 800
