"""速度读数怎么从审计里算出来。

为什么需要这么个模块：审计里每条发言都有 `ms`（这一轮跑了多久）和 `ttft_ms`
（第一个字多久到的），但**光有数字答不了"到底哪里慢"**。实测踩过的误读：

    100 条记录 → `data[-20:]` 切片 → 每家只有 3 条 → 中位数 == 最大值
    →「Codex 最长 87.2s」其实是它**这 3 条里最慢的那一条**，
    而按全体算它只排第 124/416 位。据此得出的「Codex 是瓶颈」结论是错的。

所以这里只做一件有把握的事：**用全体算，并且把用了多少条说出来**；
不够用的时候直接回 `样本不足`，而不是让调用方自己切片——切片这个动作
每次都会换个人犯同一个错。

纯 stdlib，不 import room，不碰网络。
"""

from __future__ import annotations

# 低于这个条数，中位数和最大值是一回事（样本太稀），不该拿去下结论。
MIN_SAMPLES = 20

KINDS = ("ttft", "ms")


def percentile(sorted_vals, p: float) -> float:
    """第 p 百分位（0–100），线性插值。输入必须已排序。

    为什么自己写：这个数要么用 `statistics.quantiles`（默认是 exclusive 法，
    和常见的 p90 对不上），要么引 numpy（这个仓库到现在零第三方依赖）。
    """
    vals = list(sorted_vals)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return float(vals[0])
    k = (len(vals) - 1) * max(0.0, min(100.0, float(p))) / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return float(vals[lo] + (vals[hi] - vals[lo]) * (k - lo))


def _who(row) -> str:
    """这条记录是谁的。审计行用 `agent`，turns.jsonl 用 `speaker`。

    这两个键名不一样是历史原因（审计是事件、转录是发言），但 `collect`
    同时吃两种来源。原来只查 `agent`，结果 `refresh=1` 把 turns.jsonl 读进来
    也白读——所有发言行都被 `agent` 过滤掉，界面上"按全体算"其实算的还是审计。
    """
    return str(row.get("agent") or row.get("speaker") or "")


def collect(rows, kind: str = "ms", only: str = "turn",
            agent: str = "") -> list[int]:
    """把审计行收成某个 agent 的耗时序列（毫秒，升序）。

    `only="turn"` 只算真的花了时间的行：发言（turn_end）、工具调用、被拦下、
    超时。**跳过 turn_pick / turn_grant** —— 那些是判定，耗时恒为 0，
    混进来会把中位数往 0 拽，然后界面上会出现一个谁都不信的「0.3s」。
    """
    key = "ttft_ms" if kind == "ttft" else "ms"
    out: list[int] = []
    for row in rows or []:
        if agent and _who(row) != agent:
            continue
        event = str(row.get("event") or "")
        if only == "turn" and event in ("turn_pick", "turn_grant", "context_plan"):
            continue
        val = row.get(key)
        if isinstance(val, (int, float)) and val > 0:
            out.append(int(val))
    out.sort()
    return out


def stats(rows, kind: str = "ms", agent: str = "", min_samples: int = MIN_SAMPLES) -> dict:
    """一家的一种读数。

    数字**总是算出来**（`median/p90/max`），够不够用交给 `insufficient` 标记：
    原来这里样本不足时直接回 0.0，结果轻用的房间永远是一片 0.0，
    看着像"大家都很快"，比给个带警告的真数字更误导。
    界面该做的是把 `insufficient` 的那几格标淡、写明"仅 N 条、别当结论"，
    并且**不要**让它进排行（`summarize` 里仍然拦掉）。
    """
    vals = collect(rows, kind=kind, agent=agent)
    n = len(vals)
    if not n:
        return {"agent": agent, "kind": kind, "n": 0,
                "median": 0.0, "p90": 0.0, "max": 0.0, "insufficient": True}
    return {"agent": agent, "kind": kind, "n": n,
            "median": round(percentile(vals, 50) / 1000.0, 1),
            "p90": round(percentile(vals, 90) / 1000.0, 1),
            "max": round(vals[-1] / 1000.0, 1),
            "insufficient": n < min_samples}


def summarize(rows, agents, min_samples: int = MIN_SAMPLES) -> dict:
    """给界面用的一份读数。

    「最长」永远指**全体里的最大值**，而排行也按全体算——之前那份排行是
    对 `data[-20:]` 排的，等于「最后 20 条里谁最慢」，跟"谁慢"没关系。
    """
    per: dict[str, dict] = {}
    for aid in agents or []:
        per[aid] = {
            "ms": stats(rows, "ms", aid, min_samples),
            "ttft": stats(rows, "ttft", aid, min_samples),
        }
    # 排行只排样本够的。不够的塞进去，榜面会出现「3 条样本」和「25 条样本」
    # 并排比中位数，而前者那个数基本等于它最慢的一条——榜就成了噪声排行榜。
    ranked = sorted([a for a, v in per.items() if not v["ms"]["insufficient"]],
                    key=lambda a: -per[a]["ms"]["median"])
    return {"agents": per, "ranked": ranked, "min_samples": min_samples}


def budget_left(started: float, now: float, budget_seconds: int) -> int | None:
    """本条还剩几秒。没设预算 / 不在场上 → None（界面显示「—」而不是 0）。"""
    if not budget_seconds or started <= 0:
        return None
    return max(0, int(budget_seconds - (now - started)))
