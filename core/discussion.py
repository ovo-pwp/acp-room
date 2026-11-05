"""讨论引擎 —— 【P2 归 omp，本文件是能跑的最小实现】

问题：现在只是"轮流说话 + 默认 1 轮"，所以看起来说完就散。
真相（实测 3 轮）：讨论其实能继续，但**没有阶段协议、没有共识判据**。

本文件先给出两个东西，让 P2 有地方长：

1. **阶段协议**（照抄 Detrol/quorum-cli 的 Standard 五阶段）
2. **收敛判据**（照抄 microsoft/autogen `_terminations.py` 的思路）

P2 接手后按 `PROJ/spec/P2-讨论引擎.md` 扩展，接口签名不许改。

P2 交付（omp，2026-10-31，详见 PROJ/inbox/0002-omp-P2讨论引擎.md）：
1. Standard 的 discuss 从单次改为多轮循环（上限 = Budget.max_rounds，每轮过收敛判据）；
2. Advocate / Oxford 在 run() 里实现了分边编排（评委判多数 + 魔鬼代言人；奇偶分正反方）；
3. no_progress 阈值从拍的 0.85 改为实测标定值，Discussion.trace 记录每轮重合度，CLI --trace 可打印；
4. 无进展只在交锋轮之间比对（跨阶段文本天然不同，原实现会把换阶段误判成原地打转）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from core.gui_log import log

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------- 阶段协议

from core.convergence import (  # noqa: E402  —— 纯函数与判定状态机都在这里
    ADVOCATE_ATTACK,
    ADVOCATE_DEFEND,
    ADVOCATE_JUDGE,
    ADVOCATE_PHASES,
    DISCUSS_ROUND,
    MARK_HINT,
    METHODS,
    OXFORD_PHASES,
    OXFORD_SYNTHESIS,
    STANDARD_PHASES,
    SYNTHESIS,
    Budget,
    Convergence,
    StopVerdict,
    phases_for,
    phases_that_accept_marker,
    pick_least_similar,
    signature,
    similar,
    similarity_value,
)

# 判据已搬到 core/convergence.py。这里保留同名转发是为了不动 room.py / server.py
# 的 import（它们 `from core.discussion import Budget, Discussion`）。
__all__ = ["Budget", "Convergence", "Discussion", "StopVerdict", "phases_for"]


class Discussion:
    """轮次编排 + 收敛判据。Room 持有一个，调用 run()。"""

    def __init__(self, room, method: str = "standard", budget: Budget | None = None) -> None:
        self.room = room
        self.method = method
        # 判定状态机在 core/convergence.py，不依赖 Room，可脱离宿主单测。
        # 这里只保留一个引用，编排逻辑才需要 room。
        self.conv = Convergence(budget)
        self.started_at = 0.0

    # 属性转发：外部（room/server/前端）一直在读这些名字，保持兼容。
    @property
    def budget(self) -> Budget:
        return self.conv.budget

    @budget.setter
    def budget(self, v: Budget) -> None:
        # 必须有 setter：`server.py` 每次 /api/discuss 都赋值
        # `ROOM.disc.budget = Budget(...)`。只有 getter 时这行抛
        # AttributeError，而 500 之外没有任何信号——用户只会看见
        # 「点了讨论，什么都没发生」。
        self.conv.budget = v

    @property
    def tokens_used(self) -> int:
        return self.conv.tokens_used

    @tokens_used.setter
    def tokens_used(self, v: int) -> None:
        self.conv.tokens_used = v

    @property
    def trace(self) -> list[dict]:
        return self.conv.trace

    @property
    def round_signatures(self) -> list[set[str]]:
        return self.conv.round_signatures

    @property
    def _collect(self) -> bool:
        return self.conv.collect

    @_collect.setter
    def _collect(self, v: bool) -> None:
        self.conv.collect = v

    @property
    def _markers_live(self) -> bool:
        return self.conv.markers_live

    @_markers_live.setter
    def _markers_live(self, v: bool) -> None:
        self.conv.markers_live = v

    # ---------- 判据 ----------

    def _turn_cost(self, t) -> int:
        """单条发言的真实增量 token。实现搬到 core/convergence.TokenMeter（见那里的长注释）。"""
        return self.conv.meter.cost(t)

    def should_stop(self, turns: list) -> StopVerdict:
        """turns 是这一轮的所有发言。判据与顺序见 core/convergence.py 的模块注释。"""
        return self.conv.judge(turns)

    # 这三个保留为静态转发：老的检查脚本（_p8_p2_check.py）直接调
    # Discussion._signature / Discussion._similarity_value。
    _signature = staticmethod(signature)
    _similarity_value = staticmethod(similarity_value)
    _similar = staticmethod(similar)

    async def run(self, topic: str, speakers: list[str], note: str = "") -> StopVerdict:
        """跑完整个讨论，返回停止原因。"""
        self.started_at = time.time()
        self.tokens_used = 0
        self.round_signatures.clear()
        self.trace.clear()
        # token 账本**故意不在这里清**。TokenMeter 取的是累计值差值，而
        # "第一次见到某人"那一次会按全量计费（tests/test_convergence.py 钉死了这个口径）。
        # 每场开头清零 = 每场第一轮把整个会话的历史 token 算进预算，
        # 实测 omp 一场讨论第一轮就记到 20830，直接判"token 预算用尽"停摆。
        self._markers_live = False     # 上一场可能停在生效期；每场从 answer 的不认标记状态开始

        phases = phases_for(self.method)
        log(f"讨论开始 method={self.method} phases={[p[0] for p in phases]} "
            f"speakers={speakers}")

        # running 是“整个房间是否在干活”的卫兵。user stop 会把它设成 False，
        # 所以进来第一件事就是置 True，否则任何阶段都会立刻判为“用户中止”。
        self.room.running = True
        await self.room.push_state()
        try:
            return await self._loop(phases, topic, speakers, note)
        finally:
            self.room.running = False
            await self.room.push_state()

    async def _loop(self, phases, topic: str, speakers: list[str],
                    note: str) -> StopVerdict:
        if self.method == "oxford":
            return await self._run_oxford(phases, topic, speakers)
        if self.method == "advocate":
            return await self._run_advocate(phases, topic, speakers)
        return await self._run_standard(phases, topic, speakers)

    # ---------- Standard ----------

    async def _run_standard(self, phases, topic: str, speakers: list[str]) -> StopVerdict:
        # 不与 max_rounds 取 min：min_marker_rounds > max_rounds 表示"交锋全程不认标记"
        #（观察模式）。max_rounds=1 时第 1 轮本来 < 2，同样不会生效。
        eff_min = self.budget.min_rounds_before_marker
        final_marker: StopVerdict | None = None
        for idx, (phase, parallel, instruction) in enumerate(phases):
            if not self.room.running:
                return StopVerdict(True, "用户中止", "user")
            head = f"【{topic}】\n\n" if idx == 0 and topic else ""
            if phase == "discuss":
                verdict = await self._run_discuss_rounds(topic, speakers, head, eff_min)
                if verdict:
                    return verdict
                self._markers_live = True      # 表态阶段允许终局标记
                continue
            if phase == "position":
                instruction = instruction + MARK_HINT
            turns = await self._run_phase(phase, parallel, head + instruction, speakers)
            verdict = await self._after_phase(turns, phase)
            if verdict:
                if phase != "position":
                    return verdict
                # 表态阶段的 [CONSENSUS]/[DEADLOCK] 是收敛/死锁的**确认**，不是终结：
                # 此时汇总最有价值，必须出。记下来当最终停因，流程继续走汇总。
                final_marker = verdict
                await self.room.announce(
                    f"标记已确认（{verdict.kind}），进入汇总")
        return await self._synthesis_end(speakers, topic, self.method, final_marker)

    async def _run_discuss_rounds(self, topic: str, speakers: list[str], head: str,
                                  eff_min: int = 2) -> StopVerdict | None:
        """第 3 阶段多轮循环。返回非 None 表示要提前收场（含用户中止）。

        每轮所有人按 order 串行发言（串行才能看到同轮前面人的话），轮末过收敛判据：
        [CONSENSUS]/[DEADLOCK] 标记、no_progress、token 预算、总时长。
        轮数上限 = budget.max_rounds，跑满未停则继续走表态/汇总，最终 verdict 是 completed。
        第 1 轮（准确说 eff_min 轮之前）不注入 MARK_HINT 也不认标记——实测模型见提示就抢答。
        """
        total = max(1, self.budget.max_rounds)
        self._collect = True
        try:
            for r in range(1, total + 1):
                if not self.room.running:
                    return StopVerdict(True, "用户中止", "user")
                await self.room.announce(f"阶段：discuss · 第 {r}/{total} 轮")
                live = r >= eff_min
                self._markers_live = live
                note = (head if r == 1 else "") + DISCUSS_ROUND.format(round=r, total=total) \
                    + (MARK_HINT if live else "")
                turns = await self._run_phase("discuss", False, note, speakers)
                verdict = await self._after_phase(turns, "discuss")
                if verdict:
                    return verdict
        finally:
            self._collect = False
        return None

    # ---------- Advocate ----------

    async def _run_advocate(self, phases, topic: str, speakers: list[str]) -> StopVerdict:
        final_marker: StopVerdict | None = None
        for idx, (phase, parallel, instruction) in enumerate(phases):
            if not self.room.running:
                return StopVerdict(True, "用户中止", "user")
            if phase == "challenge":
                verdict = await self._run_advocate_challenge(topic, speakers)
                if verdict:
                    return verdict
                continue
            if phase == "position":
                # 与 Standard 同一原则：标记是表态阶段的终局宣言。攻防轮不认——实测
                # 模型会在防守时只让步一半就打 [DEADLOCK]，把表态和汇总截掉。
                self._markers_live = True
                instruction = instruction + MARK_HINT
            head = f"【{topic}】\n\n" if idx == 0 and topic else ""
            turns = await self._run_phase(phase, parallel, head + instruction, speakers)
            verdict = await self._after_phase(turns, phase)
            if verdict:
                if phase != "position":
                    return verdict
                # 表态确认的标记不截断汇总（与 Standard 一致）
                final_marker = verdict
                await self.room.announce(f"标记已确认（{verdict.kind}），进入汇总")
        return await self._synthesis_end(speakers, topic, self.method, final_marker)

    async def _run_advocate_challenge(self, topic: str,
                                      speakers: list[str]) -> StopVerdict | None:
        """判多数立场 → 点名魔鬼代言人攻击（串行，让防守者看见）→ 其余人并行防守。"""
        if len(speakers) < 2:
            return None        # 一人没有"多数"可言，跳过直接进表态
        await self.room.announce("阶段：challenge · 判定多数立场")
        majority, advocate = await self._pick_advocate(topic, speakers)
        defenders = [a for a in speakers if a != advocate]
        spec = self.room.specs.get(advocate)
        await self.room.announce(
            f"阶段：challenge · 魔鬼代言人 = {spec.name if spec else advocate}，其余防守")
        head = f"【{topic}】\n\n" if topic else ""
        attack = await self.room.one_turn(
            advocate, head + ADVOCATE_ATTACK.format(majority=majority))
        verdict = await self._after_phase([attack], "challenge")
        if verdict:
            return verdict
        await self.room.announce("阶段：challenge · 其余人防守")
        defend = await self.room.parallel_round(defenders, ADVOCATE_DEFEND)
        return await self._after_phase(defend, "challenge")

    async def _pick_advocate(self, topic: str, speakers: list[str]) -> tuple[str, str]:
        """评委（第一个发言者）归纳多数立场并点名魔鬼代言人。

        解析失败时退回确定性规则：与其它人发言重合度最低的人（最边缘的声音）来当。
        返回 (多数立场文本, 代言人 agent id)。
        """
        names = [(a, self.room.specs[a].name if a in self.room.specs else a) for a in speakers]
        namelist = "、".join(n for _, n in names)
        judge = speakers[0]
        head = f"【{topic}】\n\n" if topic else ""
        turn = await self.room.one_turn(judge, head + ADVOCATE_JUDGE.format(names=namelist))
        text = turn.text or ""
        advocate = ""
        for aid, name in names:
            if aid != judge and (name in text or aid in text):
                advocate = aid
                break
        majority = ""
        m = re.search(r"多数立场[：:]\s*(\S.+)", text)
        if m:
            majority = m.group(1).strip()[:200]
        if not advocate:
            sigs = {a: signature([t for t in self.room.transcript if t.speaker == a])
                    for a in speakers}
            advocate = pick_least_similar(speakers, sigs)
            log("评委未点名或点名无效，按最小重合度兜底选魔鬼代言人")
        if not majority:
            majority = "（评委未归纳清楚，请根据上下文自行判断当前多数立场）"
        spec = self.room.specs.get(advocate)
        log(f"魔鬼代言人：{spec.name if spec else advocate}；多数立场：{majority}")
        return majority, advocate

    # ---------- Oxford ----------

    async def _run_oxford(self, phases, topic: str, speakers: list[str]) -> StopVerdict:
        """奇偶分边：偶数位（0 起）正方支持议题，奇数位反方。强制守立场，不管个人意见。

        两侧各自 parallel_round 再 gather：同侧成员看同一份上文、看不到同侧正在写的稿，
        对侧同理——辩论稿不该实时串味。用 room 公开接口拼出来，不用改 room.py。
        """
        pro = [a for a in speakers[0::2] if a in self.room.agents]
        con = [a for a in speakers[1::2] if a in self.room.agents]
        final_marker: StopVerdict | None = None

        def names(ids: list[str]) -> str:
            return "、".join(self.room.specs[a].name for a in ids) or "（无）"

        await self.room.announce(
            f"阶段：分边 · 正方[{names(pro)}] vs 反方[{names(con)}]")
        log(f"Oxford 分边：正方={pro} 反方={con}")
        head = f"【{topic}】\n\n" if topic else ""
        motion = topic or "本次议题"
        for phase, parallel, instruction in phases:   # noqa: B007
            if not self.room.running:
                return StopVerdict(True, "用户中止", "user")
            # 立论/驳论不认终局标记（立场是强制的，"共识"只可能是抢答）；
            # 结辩是终局陈词，允许认输（[CONSENSUS]）或宣布死锁（[DEADLOCK]）。
            self._markers_live = phase == "closing"
            tail = MARK_HINT if phase == "closing" else ""
            note_pro = head + f"【你是正方：支持「{motion}」。立场已指派，不管你个人真实意见，" \
                f"本回合必须守正方立场，禁止中立、禁止倒戈。】" + instruction + tail
            note_con = head + f"【你是反方：反对「{motion}」。立场已指派，不管你个人真实意见，" \
                f"本回合必须守反方立场，禁止中立、禁止倒戈。】" + instruction + tail
            jobs = []
            if pro:
                jobs.append(self.room.parallel_round(pro, note_pro))
            if con:
                jobs.append(self.room.parallel_round(con, note_con))
            results = await asyncio.gather(*jobs) if jobs else []
            turns = [t for group in results for t in group]
            verdict = await self._after_phase(turns, phase)
            if verdict:
                if phase != "closing":
                    return verdict
                # 结辩认输/死锁是终局确认，但评委总结必须出（那是 Oxford 的交付物）
                final_marker = verdict
                await self.room.announce(f"标记已确认（{verdict.kind}），进入评审总结")
        return await self._synthesis_end(speakers, topic, "oxford", final_marker)

    async def _run_phase(self, phase: str, parallel: bool, instruction: str,
                         speakers: list[str]) -> list:
        await self.room.announce(f"阶段：{phase}")
        if parallel and len(speakers) > 1:
            return await self.room.parallel_round(speakers, instruction, initiator="discussion")
        out = []
        for aid in speakers:
            if not self.room.running:
                break
            out.append(await self.room.one_turn(aid, instruction, initiator="discussion"))
        return out

    async def _after_phase(self, turns: list, phase: str) -> StopVerdict | None:
        """阶段跑完的公共出口：记 token → 过判据 → 命中就广播停因。

        停因要进 transcript 让用户看见为什么停（spec 硬要求），announce 走
        room 的公开接口，前端 p2-discuss.js 解析「讨论结束：」渲染。
        """
        self.tokens_used += sum(self._turn_cost(t) for t in turns)
        verdict = self.should_stop(turns)
        if verdict.stop:
            log(f"提前停止：{verdict.kind} — {verdict.reason}")
            await self.room.announce(f"讨论结束：{verdict.reason}")
        return verdict if verdict.stop else None

    async def _synthesis_end(self, speakers: list[str], topic: str,
                             method: str,
                             final_marker: StopVerdict | None = None) -> StopVerdict:
        """汇总收尾。Standard/Advocate 用汇总者模板，Oxford 用评委模板。

        final_marker：表态阶段确认的 [CONSENSUS]/[DEADLOCK]。汇总照出，最终停因用它——
        用户看到的不是"走完五个阶段"而是"讨论以共识/死锁结束"。
        """
        if speakers:
            await self.room.announce("阶段：synthesis · 汇总")
            synth = OXFORD_SYNTHESIS if method == "oxford" else SYNTHESIS
            tail = f"\n\n【议题】{topic}" if topic else ""
            turn = await self.room.one_turn(speakers[-1], synth + tail, initiator="discussion")
            # G1：汇总是全场信息密度最高的产物。只活一轮等于每场讨论都从零开始，
            # 尤其是 claude / codex —— 它们的 adapter 接不回上次会话（实测），
            # 没有这份记忆，它们每次进来都是空白。
            text = getattr(turn, "text", "") or ""
            if text and hasattr(self.room, "remember"):
                self.room.remember(topic, text, by=speakers[-1])
                # G6：配置开着才把结论推进 agent **自己的**长期记忆文件。
                # `auto_promote` 只管自动路径（界面上那颗按钮是人按的，另算）。
                # 这一步在 room 里过策略闸门：自动路径没人确认，闸门不放行
                # 就只是跳过并在审计里说明原因 —— 讨论引擎无权越界写别人的文件。
                promote = getattr(self.room, "promote_memory", None)
                store = getattr(self.room, "agent_memory", None)
                if promote is not None and store is not None and store.auto:
                    await promote(initiator="discussion")
        if final_marker:
            return final_marker
        n = len(phases_for(method)) + 1
        return StopVerdict(True, f"走完全部 {n} 个阶段", "completed")

# 命令行试跑入口已搬到根目录的 discuss_cli.py —— 它 import room，
# 而 core/ 不许依赖宿主（见 tests/test_architecture.py）。
# 搬走之后本文件不再引用 room，整个讨论引擎可以被单测覆盖。
