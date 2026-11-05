r"""收敛判据 —— 阶段协议 + 停不停。**不依赖 Room，可脱离宿主单测。**

## 为什么要单独一个模块

`Discussion.should_stop` 原来是 `Discussion` 的一个方法，而 `Discussion` 持有 `self.room`。
结果是：想验证"连续两轮重合度 0.9 到底会不会停"，必须起一个 Room
（连带 4 个 ACP 进程 + 读 config.json + 网络）。

但这段逻辑里**没有一行需要 Room**——它只看发言文本、token 计数、时间和预算。
把它抽出来之后，判据的每一条分支都能用几行 fixture 精确验证。

## 判据的顺序，以及为什么是这个顺序

    1. 共识/死锁标记（只在协议认可的阶段生效）
    2. token 预算
    3. 总时长
    4. 无进展（连续 N 轮论点高度重合）

**顺序不能换。** 标记排在最前，是因为它是"模型自己宣布结束"，最便宜也最明确；
预算和时长排在后面，是硬性止损；无进展排最后，因为它是三条里最可能误判的
（阈值是统计量，会错杀），让它当最后一道。

## 两个从实测得来的常量，别随手改

- `min_rounds_before_marker = 2`
  实测：第 1 轮就注入 `[CONSENSUS]` 提示时，模型会抢着宣布收敛
  （"远程办公"校准场第 1 轮就被 Codex 收敛），多轮交锋形同虚设。
  第 1 轮只交锋不认标记，第 2 轮起才提示并生效。

- `no_progress_threshold = 0.45`
  原来拍的 0.85。实测标定（0002 交付件，全部贴原始输出）：
    真实活跃交锋轮相邻重合度：0.123 / 0.104 / 0.108 / 0.113 / 0.139
    合成复读/微改写轮：0.667 / 0.947
  0.45 落在 0.139 和 0.667 之间的空档里，偏保守：宁可多谈一轮，也不错杀还在产新的讨论。
  **已知局限**：实测五场都被标记判据先命中，`no_progress` 没在真实数据上触发过。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.gui_log import log

# ---------------------------------------------------------------- 阶段协议

STANDARD_PHASES: list[tuple[str, bool, str]] = [
    # (阶段名, 是否并行, 给 agent 的额外指令)
    ("answer", True,
     "【阶段 1/5 · 独立作答】其他人也在同时回答，你看不到他们的答案。"
     "给出你自己的完整回答，不要猜测别人会说什么。"),
    ("critique", True,
     "【阶段 2/5 · 互相挑错】现在你看到了所有人上一阶段的答案。"
     "只做一件事：指出其中哪一条是错的、缺什么证据、在哪一步会撞墙。不要重复你自己的答案。"),
    ("discuss", False,
     "【阶段 3/5 · 自由交锋】直接回应最该被反驳的那一句。可以改判，但要说清哪一点说服了你。"),
    ("position", True,
     "【阶段 4/5 · 表态】给出你的最终立场 + 置信度（0-100%）。"
     "如果你改判了，明确写「我改判」。如果还有分歧，写出分歧的唯一分歧点。"),
]

SYNTHESIS = (
    "【阶段 5/5 · 汇总】你是汇总者。只输出三段，不要客套：\n"
    "1. **共识**：大家实际同意的结论是什么（一句话）\n"
    "2. **分歧**：还剩哪些分歧，各自理由（最多 3 条）\n"
    "3. **下一步**：基于以上，具体该做什么（可执行）"
)

# 终局标记提示。**只在认标记的阶段追加**（见 phases_that_accept_marker）：
# 实测第 1 轮就注入时，模型会抢着打 [CONSENSUS] 把讨论截掉。
MARK_HINT = (
    "\n\n[终局标记] 这一轮你可以结束整场讨论，但只在**真的到底了**的时候用：\n"
    "  · 确实收敛 → 单独一行写 `[CONSENSUS]`\n"
    "  · 确实卡在互斥前提上、再谈也不动 → 单独一行写 `[DEADLOCK]`，并写出那个前提\n"
    "还在推进就一个字都别打。抢着宣布收敛比谈不完更糟。"
)

# 自由交锋阶段的每轮指令（{round}/{total} 由调用方填）。
DISCUSS_ROUND = (
    "【阶段 3/5 · 自由交锋 第 {round}/{total} 轮】"
    "不要复述自己上一轮说过的话。挑现场**最站得住但被当成显然**的那一句正面回应："
    "要么给出它经不起的条件，要么给出更强的一条。\n"
)

# 魔鬼代言人玩法（advocate）。三段都是给模型看的，输出形状被
# `Discussion._pick_advocate` 的解析依赖：`多数立场：` 这行必须照格式。
ADVOCATE_JUDGE = (
    "【评委】房间里这些人（{names}）刚各自表过态。只做两件事，各一行，不要评论自己：\n"
    "1. 归纳出当前多数立场，照这个格式输出（冒号后写具体内容）：`多数立场：xxx`\n"
    "2. 从名单里点名一个人当魔鬼代言人，挑**立场离多数最远**的那个，"
    "直接写他的名字或 id。没有明显边缘者就点名发言最少的那个。"
)

ADVOCATE_ATTACK = (
    "【魔鬼代言人】本轮多数立场是：{majority}。\n"
    "你的任务是攻击它：给出最强的反例、没说出口的前提、以及一个会让它当场失效的场景。"
    "不需要你自己同意这个反驳，也不需要给替代方案——只负责把它打到需要回应的位置。"
)

ADVOCATE_DEFEND = (
    "【防守】刚才有人专门攻击了多数立场。逐条回应他给的场景："
    "哪些确实成立、你的说法要在哪一处加限定。"
    "只让**具体那一步**，不许整体投降；改判了就明写「我改判」以及改在哪。"
)

OXFORD_SYNTHESIS = (
    "【评委总结】你是这场辩论的评委，两边立场是抽签分配的，不代表你的意见。"
    "只输出三段，不要和稀泥：\n"
    "1. **胜负点**：哪一方在哪个具体论点上压住了对方（说不出就写「没有」）\n"
    "2. **真分歧**：双方实际分歧卡在哪一个前提上（不要写成「双方都有道理」）\n"
    "3. **待验证**：这场辩论没能解决、需要外部证据才能定的那一件事"
)

ADVOCATE_PHASES: list[tuple[str, bool, str]] = [
    ("answer", True, STANDARD_PHASES[0][2]),
    ("critique", True, STANDARD_PHASES[1][2]),
    # 魔鬼代言人环节不走通用 _run_phase：先判多数立场、点名代言人攻击，其余人并行防守。
    ("challenge", False, ""),
    ("position", True, STANDARD_PHASES[3][2]),
]

OXFORD_PHASES: list[tuple[str, bool, str]] = [
    ("opening", True, "【立论】按分配给你的立场（正方/反方）给出最强论证。不要中立。"),
    ("rebuttal", True, "【驳论】直接攻击对方刚才的论点，逐条回应，不要重复自己的立论。"),
    ("closing", True, "【结辩】总结你方最强的点，指出对方未回答的问题。"),
]

METHODS = {
    "standard": STANDARD_PHASES,
    "advocate": ADVOCATE_PHASES,
    "oxford": OXFORD_PHASES,
}


def phases_for(method: str) -> list[tuple[str, bool, str]]:
    """未知方法回退 standard —— 回退而不是报错，因为方法名是配置项，写错不该让房间停摆。"""
    return METHODS.get(method, STANDARD_PHASES)


def standard_phase_count(method: str) -> int:
    """走完全部阶段时的"几个阶段"（含汇总）。用于拼接停因文案。"""
    return len(phases_for(method)) + 1


def phases_that_accept_marker() -> set[str]:
    r"""哪些阶段允许模型用 `[CONSENSUS]` / `[DEADLOCK]` 宣布终局。

    answer/critique 不认：这两个阶段模型信息最少，最容易抢答。
    表态、攻防、结辩、后续交锋轮才认 —— 那时协议已经给足了信息。
    """
    return {"discuss", "position", "challenge", "closing"}


# ---------------------------------------------------------------- 停因与预算


@dataclass
class StopVerdict:
    stop: bool
    reason: str = ""
    kind: str = ""      # consensus | deadlock | max_rounds | token_budget | no_progress | timeout | user | completed


@dataclass
class Budget:
    max_rounds: int = 4
    max_tokens: int = 200_000
    max_seconds: int = 900
    no_progress_rounds: int = 2
    min_rounds_before_marker: int = 2
    no_progress_threshold: float = 0.45


CONSENSUS_RE = re.compile(r"\[(CONSENSUS|DEADLOCK)\]", re.I)


# ---------------------------------------------------------------- 论点指纹


def signature(turns: Sequence[Any]) -> set[str]:
    """粗略的论点指纹：中文按二字组、英文按词，取去重后的集合。

    为什么是二字组而不是分词：不想为一个"判断有没有新论点"的需求引入 jieba
    这样的依赖，而中文二字组在这个粗粒度上足够——实测活跃轮与复读轮的
    重合度分布（0.1 段 vs 0.7 段）分得很开。
    """
    text = " ".join((getattr(t, "text", "") or "") for t in turns)
    cjk = set(re.findall(r"[\u4e00-\u9fff]{2}", text))
    words = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", text.lower()))
    return cjk | words


def similarity_value(a: set[str], b: set[str]) -> float:
    """Jaccard 重合度。空集合返回 0（不是 1）——没有论点不等于论点相同。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similar(a: set[str], b: set[str], threshold: float = 0.68) -> bool:
    return similarity_value(a, b) >= threshold


def pick_least_similar(speakers: Sequence[str],
                       sigs: dict[str, set[str]]) -> str | None:
    """选魔鬼代言人：立场**最孤立**的那个。

    评委没点名或点了个无效名字时的兜底。确定性规则，不额外调模型。

    ## 「最孤立」的度量

        isolation(a) = max( 与其它任何有立场成员的重合度 )

    即"你和**最像你的那个人**有多像"。这个值越小，说明越没人替你说话，
    越适合去攻击多数立场。

    ## 这里修过一个真 bug

    原实现写的是 `min(与其它成员的重合度)`。看起来像"跟谁都不像"，
    实际是"跟**最不像**的那个人不像"——而只要群里存在任何一对不像的成员，
    所有人都会拿到 0.00：

        pi     min(1.0, 0.0) = 0.00
        omp    min(1.0, 0.0) = 0.00
        codex  min(0.0, 0.0) = 0.00

    三个并列 0，`min()` 按顺序取第一个，于是"兜底选人"**永远退化成选第一个发言人**。
    这个 bug 不会报错，只会让魔鬼代言人的挑选悄悄失去意义——所以它值得一条测试。

    ## 没立场的人不参与评选

    签名是空的（没发过言 / 全是短句），说明拿不到它的立场信号，
    让它去当魔鬼代言人只是随机。这类人排在最后，只有在所有人都没立场时才轮到。
    """
    if not speakers:
        return None

    def isolation(a: str) -> tuple[int, float]:
        mine = sigs.get(a) or set()
        if not mine:
            return (1, 0.0)                      # 没立场：优先级最低
        others = [s for x, s in sigs.items() if x != a and s]
        if not others:
            return (0, 0.0)                      # 只有它一个有立场，无从比较
        return (0, max(similarity_value(mine, s) for s in others))

    return min(speakers, key=isolation)


@dataclass
class TokenMeter:
    r"""按发言人记上一次**累计**读数，取差值当本轮成本。

    实测（0002 交付件）：ACP 的 `usage.totalTokens` 是**会话累计值**——
    同一 CLI 进程里 omp 三轮 12852→13686→16219、claude 18235→19136→20439、
    codex 12573→13618→15686 全部单调递增；pi 的 agent 恒报 0。

    原实现把每轮累计值直接相加，会双计（"1+1等于几"12 条发言报了 142k）。
    差值为负说明会话被重建，退回全量。报 0 的贡献 0（会计口径缺口）。
    """

    _last: dict[str, int] = field(default_factory=dict)

    def cost(self, turn: Any) -> int:
        cum = int(getattr(turn, "tokens", 0) or 0)
        if cum <= 0:
            return 0
        who = getattr(turn, "speaker", "")
        prev = self._last.get(who, 0)
        delta = cum - prev if cum >= prev else cum
        self._last[who] = cum
        return delta

    def total(self, turns: Sequence[Any]) -> int:
        return sum(self.cost(t) for t in turns)


# ---------------------------------------------------------------- 判定状态机


class Convergence:
    """收敛判定。持有判定所需的全部状态，**不引用 Room**。

    `collect` 与 `markers_live` 由编排循环设置（它们描述的是"当前在哪个阶段"），
    其余状态由本类自己维护。
    """

    def __init__(self, budget: Budget | None = None) -> None:
        self.budget = budget or Budget()
        self.tokens_used = 0
        self.started_at = 0.0
        self.round_signatures: list[set[str]] = []
        self.trace: list[dict] = []
        self.meter = TokenMeter()
        # 无进展比对只在交锋轮之间进行：answer→critique 换阶段文本必然不同，
        # 跨阶段比会把正常推进误判成原地打转（原实现就吃这个亏）。
        self.collect = False
        # 标记是否生效。见 phases_that_accept_marker()。
        self.markers_live = False

    # ---------- 成本 ----------

    def charge(self, turns: Sequence[Any]) -> int:
        """把这一轮的增量 token 记进总账，返回本轮花费。"""
        n = self.meter.total(turns)
        self.tokens_used += n
        return n

    # ---------- 判据 ----------

    def judge(self, turns: Sequence[Any], now: float | None = None) -> StopVerdict:
        """turns 是这一轮的所有发言。返回停不停、为什么。"""
        if not turns:
            return StopVerdict(False)

        now = time.time() if now is None else now

        # 0. 先记签名（放在标记检查之前，保证每个交锋轮都留 trace）
        if self.collect:
            self._record_signature(turns)

        # 1. 共识 / 死锁标记
        if self.markers_live:
            v = self._marker_verdict(turns)
            if v:
                return v

        # 2. token 预算
        if self.tokens_used >= self.budget.max_tokens:
            return StopVerdict(
                True,
                f"token 预算用尽（{self.tokens_used}/{self.budget.max_tokens}）",
                "token_budget")

        # 3. 总时长（started_at==0 说明没经过 run()，无从计时，跳过）
        if self.started_at:
            elapsed = now - self.started_at
            if elapsed >= self.budget.max_seconds:
                return StopVerdict(True, f"超时（{elapsed:.0f}s/{self.budget.max_seconds}s）",
                                   "timeout")

        # 4. 无进展
        v = self._no_progress_verdict()
        if v:
            return v

        return StopVerdict(False)

    def _record_signature(self, turns: Sequence[Any]) -> None:
        sig = signature(turns)
        prev = self.round_signatures[-1] if self.round_signatures else None
        sim = similarity_value(prev, sig) if prev else 0.0
        self.round_signatures.append(sig)
        self.trace.append({
            "round": len(self.round_signatures),
            "jaccard_vs_prev": round(sim, 3),
            "new_terms": len(sig - prev) if prev else None,
        })
        log(f"交锋轮 #{len(self.round_signatures)}：与上一轮重合度 {sim:.2f}"
            f"（阈值 {self.budget.no_progress_threshold}）")

    @staticmethod
    def _marker_verdict(turns: Sequence[Any]) -> StopVerdict | None:
        for t in turns:
            m = CONSENSUS_RE.search(getattr(t, "text", "") or "")
            if not m:
                continue
            word = m.group(1).upper()
            who = getattr(t, "name", "") or "?"
            if word == "CONSENSUS":
                return StopVerdict(True, f"{who} 打出 [CONSENSUS]，讨论已收敛", "consensus")
            return StopVerdict(True, f"{who} 打出 [DEADLOCK]，确认分歧无法消解", "deadlock")
        return None

    def _no_progress_verdict(self) -> StopVerdict | None:
        if not self.collect:
            return None
        n = self.budget.no_progress_rounds
        th = self.budget.no_progress_threshold
        if len(self.round_signatures) < n:
            return None
        recent = self.round_signatures[-n:]
        if all(similar(recent[0], s, th) for s in recent[1:]):
            return StopVerdict(
                True,
                f"连续 {n} 轮没有新论点（重合度 ≥ {th}），判定原地打转",
                "no_progress")
        return None

    # ---------- 可观测 ----------

    def summary(self) -> dict:
        return {
            "tokens_used": self.tokens_used,
            "rounds_measured": len(self.round_signatures),
            "trace": list(self.trace),
            "collect": self.collect,
            "markers_live": self.markers_live,
        }


# 整轮看门狗的绝对上限。超过半小时的一轮一定是卡住了，不是慢。
WATCHDOG_CAP = 1800

# 下限。不能算出 0——0 秒超时会让每一轮"立刻僵住"，房间彻底不可用。
# 配置里 turn_timeout 写成 0（当成"不限"的人很多）时，必须有个兜底。
WATCHDOG_FLOOR = 60


def watchdog_seconds(turn_timeout: int, explicit: int | None = None) -> int:
    """算整轮看门狗的超时秒数。

    `turn_timeout` 只约束**单次** prompt。它管不了"整轮卡死"：只要有一个
    await 永不返回（ACP 不回包、子进程僵住），`run()` 就永不返回，
    它的 `finally` 也就永不执行，房间锁死到重启为止。实测卡过 16 分钟。

    默认取 `turn_timeout × 2`，再封顶 `WATCHDOG_CAP`。

    为什么不是 `turn_timeout × 步数`：8 步 × 900s = 2 小时，那等于没有看门狗。
    真卡住了，用户要瞪着转圈两小时才知道。正常一轮（8 步、每步十几秒）
    两分钟就跑完了；超过半小时一定是哪里不对——这时候放开房间比继续等更对，
    用户至少能停、能重来。
    """
    if explicit:
        return int(explicit)
    return max(WATCHDOG_FLOOR, min(int(turn_timeout) * 2, WATCHDOG_CAP))
