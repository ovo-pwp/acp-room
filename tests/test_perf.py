"""core/perf.py 的单测 —— 速度读数怎么算，以及什么时候**不该**算。

钉住的是踩过的那个错：拿 `data[-20:]` 切片算中位数，每家只剩 3 条，
中位数 == 最大值，于是「Codex 最长 87.2s」被当成结论，而按全体 416 条算
它只排第 124 位、中位数比 Pi 还快。这个错不会报错，只会让人误判瓶颈，
所以「样本不足时不许排名」这条必须有用例锁住。
"""

import pytest

from core.perf import MIN_SAMPLES, budget_left, collect, percentile, stats, summarize

R = ["pi", "omp", "claude", "codex"]


def row(agent, ms, ttft=0, event="tool_call"):
    return {"agent": agent, "ms": ms, "ttft_ms": ttft, "event": event}


def rows_for(agent, vals, **kw):
    return [row(agent, v, **kw) for v in vals]


# ---------------------------------------------------------------- percentile

@pytest.mark.parametrize("vals,p,expect", [
    ([50], 50, 50.0),
    ([10, 20], 50, 15.0),                 # 线性插值，不是"取中间那个"
    ([0, 100], 90, 90.0),
    ([1, 2, 3, 4, 5], 0, 1.0),            # 端点夹紧
    ([1, 2, 3, 4, 5], 100, 5.0),
    ([1, 2, 3, 4, 5], 250, 5.0),          # 越界当 100 处理，不抛
    ([], 50, 0.0),
])
def test_percentile(vals, p, expect):
    assert percentile(vals, p) == pytest.approx(expect)


# ---------------------------------------------------------------- collect

def test_collect_丢掉判定类事件():
    """turn_pick / turn_grant / context_plan 的 ms 恒为 0。
    混进来的话中位数会被拽到 0，界面上就是一个谁都不信的「0.3s」。"""
    rows = [row("pi", 5000), row("pi", 0, event="turn_pick"),
            row("pi", 0, event="turn_grant"), row("pi", 0, event="context_plan")]
    assert collect(rows, agent="pi") == [5000]


def test_collect_忽略非正数和坏值():
    rows = [{"agent": "pi", "ms": 0}, {"agent": "pi", "ms": None},
            {"agent": "pi", "ms": -5}, {"agent": "pi", "ms": "12"},
            row("pi", 300)]
    assert collect(rows, agent="pi") == [300]


def test_collect_按agent过滤():
    rows = rows_for("pi", [1000]) + rows_for("codex", [9000])
    assert collect(rows, agent="codex") == [9000]
    assert collect(rows, agent="不存在的") == []


def test_collect_ttft读的是另一个字段():
    rows = [{"agent": "pi", "ms": 9000, "ttft_ms": 1200, "event": "tool_call"}]
    assert collect(rows, kind="ttft", agent="pi") == [1200]
    assert collect(rows, kind="ms", agent="pi") == [9000]


def test_collect_转录里的行也算进来():
    """转录行用 `speaker`，审计行用 `agent`。/api/perf 的 refresh=1 就是把
    turns.jsonl 拼进来补「发言不写审计」那块缺口——只认 `agent` 的话，
    拼进来也全被过滤掉，等于没补。"""
    rows = [{"speaker": "pi", "name": "Pi", "text": "说一句", "ms": 7000,
             "ttft_ms": 900, "status": "ok"}]
    assert collect(rows, agent="pi") == [7000]
    assert collect(rows, kind="ttft", agent="pi") == [900]
    assert collect(rows, agent="codex") == []


def test_collect_人类发言不算进任何agent():
    rows = [{"speaker": "human", "ms": 1234}]
    assert collect(rows, agent="pi") == []


# ---------------------------------------------------------------- stats

def test_stats_样本不足照样给真数字():
    """3 条时中位数=最大值，看起来像结论——所以拦的是**排名**，不是数字。

    原来这里样本不足直接回 0.0。轻用的房间永远是一片 0.0，界面看着像
    「大家都很快」，比标了警告的真数字更误导。
    """
    s = stats(rows_for("codex", [3000, 4000, 87200]))
    assert s["insufficient"] is True
    assert s["n"] == 3
    assert s["median"] == pytest.approx(4.0)      # 全体 3 条算出来的，不是 0
    assert s["max"] == pytest.approx(87.2)
    assert s["p90"] == pytest.approx(70.6)


def test_stats_一条都没有才给0():
    s = stats(rows_for("codex", [3000]), "ms", "pi")   # 名册对上，一条也没收到
    assert s == {"agent": "pi", "kind": "ms", "n": 0,
                 "median": 0.0, "p90": 0.0, "max": 0.0, "insufficient": True}


def test_stats_够样本才报数():
    vals = list(range(1, MIN_SAMPLES + 1))          # 1s..20s，单位秒→毫秒在下面
    s = stats(rows_for("pi", [v * 1000 for v in vals]))
    assert s["insufficient"] is False
    assert s["n"] == MIN_SAMPLES
    # 秒，一位小数
    assert s["median"] == pytest.approx(10.5)
    assert s["max"] == pytest.approx(20.0)
    assert s["p90"] == pytest.approx(18.1)


# ---------------------------------------------------------------- summarize

def test_summarize_排行只排样本够的():
    """codex 只跑了 3 条，其中一条 87s。按切片算它是"最慢的人"，
    按全体它连榜都上不了——这条就是这个区别。"""
    rows = (rows_for("codex", [3000, 4000, 87200])
            + rows_for("pi", [25000] * MIN_SAMPLES)
            + rows_for("omp", [8000] * MIN_SAMPLES))
    out = summarize(rows, R)
    assert out["ranked"] == ["pi", "omp"]           # 全体中位数大的在前
    assert out["agents"]["codex"]["ms"]["insufficient"] is True
    assert "codex" not in out["ranked"]
    assert "claude" in out["agents"]                # 一家没有也占一行
    assert out["agents"]["claude"]["ms"]["n"] == 0


def test_summarize_没有agent时不炸():
    out = summarize([], [])
    assert out["agents"] == {} and out["ranked"] == []


def test_summarize_排行用的不是最近几条():
    """全体 25 条里前 20 条慢、后 5 条快 —— 切片法会得出"这家很快"。"""
    rows = rows_for("pi", [30000] * 20 + [1000] * 5)
    out = summarize(rows, ["pi"])
    assert out["ranked"] == ["pi"]
    assert out["agents"]["pi"]["ms"]["median"] > 20  # 按全体算，不是尾部那 5 条


# ---------------------------------------------------------------- budget_left

@pytest.mark.parametrize("started,now,budget,expect", [
    (0, 100, 90, None),        # 没设预算 → None（界面显示「—」而不是 0）
    (100, 110, 0, None),
    (100, 130, 90, 60),
    (100, 200, 90, 0),         # 超了也不给负数
])
def test_budget_left(started, now, budget, expect):
    assert budget_left(started, now, budget) == expect
