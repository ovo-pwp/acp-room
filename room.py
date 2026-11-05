"""Room —— 一个房间，人和若干 ACP agent 在里面依次发言。

关键设计（想清楚了才写，改起来才不会推倒重来）：

1. **每个 agent 有自己的 ACP session，一直接着用。** ACP 的 session 本身就是有状态的，
   agent 记得自己说过什么、做过什么。所以平台不需要把整份 transcript 反复灌给它——
   只把「它上次发言之后别人说了什么」推进去。这就是 ACP 比"每次都重发全文"强的地方：
   上下文不重复计费，也不会因为窗口滑掉而失忆。

2. **发言顺序是数据**（`order` 列表），不是状态机。加人/静音/换顺序都不改代码。

3. **平台不做文件 IO、不跑命令。** 不声明 fs/terminal 能力，agent 用它自己的工具。
   平台只做传话 + 显示 + 权限转达。这样接进来的 agent 该有什么本事就有什么本事。

4. **同一轮内并行，轮与轮之间串行。** 顺序执行看着安全，实际很蠢：Pi 2s + OMP 12s +
   Claude 13s，轮到 Codex 时已经过去 27 秒，加上它自己 16 秒，用户要为一句回复等 43 秒。
   并行后总耗时 = 最慢那个（16s），代价是同轮内 agent 看不到彼此——但这正好对：
   一轮所有人看同一份上下文，下一轮再看上一轮的结果，这才是正常的群聊节奏。
   如果需要真串行（一个接一个接话），把 config 里 `parallel_turns` 设成 false。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.agent import AcpAgent, AcpError, AgentConfig, text_of_update
from core.agent_memory import AgentMemory
from core.agent_memory import summarize as am_summarize
from core import context as ctx
from core import convergence
from core import reply_guard
from core import turntaking as tt
from core.attachments import Attachments, AttachmentError
from core.arbitration import arbitrate
from core.audit import Audit
from core.bench import Bench
from core.config_opts import ConfigStore
from core.discussion import Budget, Discussion
from core.gui_log import log
from core.memory import Memory
from core.policy import Decision as PolicyDecision
from core.policy import Policy
from core.policy import Subject as PolicySubject
from core.skills import Skills
from core.store import Store

# 房间规则。每轮都发，因为 agent 不共享上下文。
# 没有这段，编程助手会把人话当成待办：实测 Claude 一轮 70 万 token 并开始改文件。
#
# 第二条是实测出来的：Codex 会先“看一眼咱们讨论的是什么平台”再回答，
# 一回跑 54 秒，还燒了 2.3 万 token。告诉它“背景已在上下文里，别去看文件”能直接省掉。
# 「这轮我没话说」标记。抄 dsh-roundtable 的 `wait`：只停本轮，不插话。
#
# 为什么需要：现在 agent 被点名就必须产出正文，于是它偶尔会写一段
# "我同意上面说的，补充一点……"——纯填充，既烧 token 又把有效信号稀释掉。
# 给它一个明确的退出口，比逼它产废话便宜得多。
#
# 只在**正文以标记开头**时生效（前面允许有空白和 markdown 加粗），
# 不匹配正文中间出现的同名字样——否则 agent 引用别人说过的话就会误触发。
WAIT_MARK = "[WAIT]"
_WAIT_RE = re.compile(r"^\s*\**\s*\[WAIT\]\s*\**\s*", re.I)

# 退出口。**必须是有条件的**。
# 上一版无条件给出口，后果实测在审计里：被点名的 omp 回「这条点名 @omp，不是我」，
# 6 条 turn_grant 换来 6 条 turn_wait。给了出口却不说"什么时候不能用"，
# 模型就会用它来躲开它不确定的事。
WAIT_HINT = (
    "\n\n[什么时候可以不说]\n"
    "只有在**这轮没有点你的名**、且你唯一能说的只是附和或复述别人时，"
    f"才可以不写正文，直接输出 {WAIT_MARK}（可附一句不超过 20 字的理由）。\n"
    "**如果这轮在叫你，或者你有别人没说到的实质内容，就必须回答。**"
)

PREAMBLE = """[房间规则 · 每次发言都适用]
你在一个多 AI 聊天室里。房间里有一个人类和几个不同的 AI，大家轮流说话。

【关于背景】你已经在同一个会话里，上下文就是前面那些发言。**不要为了“了解情况”去读文件、
查目录、跑命令**——这里没有需要你去勘实的项目。如果需要背景，直接问人类。

1. **这是聊天，不是派活。** 没人让你执行代码任务就不要动手：不改文件、不跑命令、
   不建目录、不写测试。除非人类明确说「去做/改代码/跑一下」。
2. **只说你说的话。** 不加 `[名字]:` 前缀，不加客套开场，不复述别人刚说过的。
3. **接着推进。** 可以质疑、补充具体例子、指出别人在哪一步会撞墙。不同意就直说，
   并说清你反对的是哪一句。
4. **简洁。** 默认 150 字以内；确实需要展开就先给结论。
5. **不编造。** 没验证过的事说「没验证」，不要用听起来确定的语气。
6. 不要重复输出你上一条消息已经说过的内容。"""


@dataclass
class Turn:
    seq: int
    speaker: str        # agent id 或 "human"
    name: str
    text: str
    ts: float
    tokens: int = 0
    status: str = "ok"  # ok | error | cancelled | wait（agent 主动放弃本轮）
    ms: int = 0         # 这一轮花了多久
    # 这一轮是哪个 ACP 会话产出的。换模型/重启/进程崩过都会换 sessionId，
    # 而 sessionId 一变就意味着 agent 侧上下文清零——光看正文看不出来。
    session: str = ""
    # 这一轮用的是哪个模型（agent 当前 configOptions 里的 model 值）。
    # 同一个人一小时内可能换了三次模型，而正文里看不出差别；复盘「这句为什么
    # 这么蠢」时，这个字段是唯一线索。
    model: str = ""
    # 附加的图片：[{id, mime, bytes, url, name}]。
    # 只存元数据，不存 base64——否则 snapshot() 每轮都背着几 MB 往外推。
    attachments: list[dict] = field(default_factory=list)


# 已知噪音：ACP adapter 会把自家启动警告当正文流出来，前端看见就是一堆红字。
# 用完整句式匹配（不靠 ". " 切，因为警告后面可能紧跟真正的回答），
# 只在开头反复剥，中间和末尾一律不动。
_NOISE_RE = [
    # codex: "Warning: Model metadata for `x` not found. ... issues."
    re.compile(r"^\**Warning:\**\s*Model metadata for.*?issues\.\s*", re.I | re.S),
    # claude: "**Warning:** Git Bash wasn't found, ... written for PowerShell."
    re.compile(
        r"^\**Warning:\**\s*Git Bash wasn'?t found.*?(?:written for PowerShell|args)\.\s*",
        re.I | re.S,
    ),
]


def clean_text(text: str) -> str:
    """去掉 ACP adapter 泄到正文里的启动警告，只动开头。"""
    if not text:
        return text
    body = text.lstrip()
    for _ in range(4):
        before = body
        for rx in _NOISE_RE:
            body = rx.sub("", body, count=1).lstrip()
        if body == before:
            break
    return body.strip()


class Room:
    @staticmethod
    def _resolve_cwd(raw: str, root: Path) -> str:
        """把 cwd 解析成绝对路径。相对路径按仓库根算，不按进程当前目录。

        理由：模板里写的是 "."。如果按进程 cwd 解析，从不同目录启动服务，
        agent 就会在完全不同的地方读写文件，而日志里只显示 "cwd=."，
        看半天也发现不了。绝对路径则一眼能看懂。
        """
        p = Path(str(raw))
        if not p.is_absolute():
            p = root / p
        return str(p.resolve())

    def __init__(self, root: Path) -> None:
        self.root = root
        # 首次跑最常见的一跤：忘了把 config.example.json 复制成 config.json。
        # 裸 FileNotFoundError 只会说"找不到文件"，新用户不知道该抄哪个模板，
        # 所以这里给一条能直接照做的话。
        cfg_path = root / "config.json"
        if not cfg_path.exists():
            example = root / "config.example.json"
            hint = (f"先把模板复制一份改名：\n"
                    f"    copy config.example.json config.json\n"
                    f"（模板在 {example}）") if example.exists() else \
                   "仓库里没有 config.example.json，请检查是否下载完整。"
            raise SystemExit(f"缺少 {cfg_path}\n{hint}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        # 相对路径按**仓库根**解析，而不是按进程的当前目录。
        # 模板里 cwd 是 "."，如果按进程 cwd 解析，从别的目录启动服务
        # 会让 agent 在完全不同的地方读写文件——这种差异很难在日志里看出来。
        self.cwd: str = self._resolve_cwd(cfg.get("cwd") or ".", root)
        self.turn_timeout: int = cfg.get("turn_timeout", 900)

        self.agents: dict[str, AcpAgent] = {}
        self.specs: dict[str, AgentConfig] = {}
        for a in cfg["agents"]:
            if not a.get("enabled", True):
                continue
            spec = AgentConfig(
                id=a["id"],
                name=a.get("name", a["id"]),
                cmd=list(a["cmd"]),
                # 每个 agent 可以覆盖 cwd；同样按仓库根解析，保持和顶层一致
            cwd=self._resolve_cwd(a.get("cwd") or self.cwd, self.root),
                color=a.get("color", "#888888"),
                permission=a.get("permission", "ask"),
                model_hint=a.get("model_hint", ""),
                env=a.get("env") or {},
                role=a.get("role", ""),
                tag=a.get("tag", ""),
                style=a.get("style", ""),
            )
            self.specs[spec.id] = spec
            self.agents[spec.id] = AcpAgent(spec, self._on_update, self._on_permission)

        self.order: list[str] = list(self.specs)
        self.transcript: list[Turn] = []
        self.seq = 0
        self.running = False
        self.task: asyncio.Task | None = None
        self.parallel: bool = cfg.get("parallel_turns", True)
        # agent -> 它已经"看到"到第几条（transcript 下标）
        self.seen: dict[str, int] = {aid: 0 for aid in self.agents}
        # 本次进程里真被 prompt 过的 agent。落盘会话 id 要用它把关：
        # 一个从没开过口的会话在 agent 侧根本没有可恢复的东西
        # （codex 只有跑完一轮才写 rollout 文件），把这种 id 存下来，
        # 下次启动必然 resume 失败，还会顺手覆盖掉真正还有 rollout 的旧 id。
        self.spoke: set[str] = set()
        self.sockets: set[Any] = set()
        self.perm_waiters: dict[str, asyncio.Future] = {}
        self.perm_seq = 0
        self.status: dict[str, str] = {aid: "未连接" for aid in self.agents}
        # 每个 agent 正在流式输出的内容，用于界面实时显示
        self.streaming: dict[str, str] = {}
        # P6 修复：toolCallId -> {tool, title}。tool_call_update 不带 kind/title，
        # 靠它把结果对回发起时的名字，否则审计里全是 unknown。
        self.inflight_tools: dict[str, dict] = {}

        # 落盘。重启后会自动把历史读回来。
        self.store = Store(root, cfg.get("room", "main"))
        # G2：记住每个 agent 上次的 ACP sessionId。没有它的话，平台侧历史回来了、
        # agent 侧却是全新会话 —— 表现为"你明明说过，它却一脸茫然"。
        self.saved_sessions: dict = self.store.load_sessions()
        for aid, agent in self.agents.items():
            agent.resume_session_id = self.saved_sessions.get(aid)
        self.audit = Audit(root, cfg.get("room", "main"))
        # G1：房间记忆。讨论汇总那段结论写在这里，下一轮发回给每个 agent。
        self.memory = Memory(root, cfg.get("room", "main"))
        # G6：把上面那些结论推进 agent **自己的**长期记忆文件。
        # 房间记忆是每轮随 prompt 发过去的，agent 一关就不在它脑子里了；
        # 落点和格式各家不同（见 core/agent_memory.py），自动写默认关着。
        # `home` 只为测试存在（单测不能往真的 ~/.claude 里写），线上不配就是 $HOME。
        self.agent_memory = AgentMemory(
            root, cfg.get("room", "main"), (cfg.get("agent_memory") or {}),
            agent_ids=list(self.specs), home=str(cfg.get("home") or ""))
        # 算力基准（P16 面板的 tok/s 与上下文两列）。不阻塞主流程。
        self.bench = Bench(root, cfg.get("room", "main"), (cfg.get("bench") or {}))

        # 策略闸门。自己读 config.json 的 policy 块；没配就无条件 deny（fail-closed）。
        # 放在 audit 之后，因为判定完要立刻写审计。
        self.policy = Policy()
        log(self.policy.summary())

        # 图片附件。字节落 data/<room>/attachments/，正文只存 id。
        self.attachments = Attachments(root, cfg.get("room", "main"))

        # 谁发起的这一轮：person（人打字）| discussion（讨论引擎自动跑）。
        # one_turn / parallel_round 的调用方负责设置，权限判定和审计都读它。
        self._initiator: str = "person"
        # 这一轮被**点名**的 agent。被点名的人不许用退出口 —— 见 core/persona.py。
        self._addressed: set[str] = set()

        # 发言顺序模式。
        #   round —— 按名册轮，同一轮内并行（旧行为）
        #   free  —— 顺序由对话决定：上一句点名谁、谁就接话（见 core/turntaking.py）
        conv = cfg.get("conversation") or {}
        self.conv_mode: str = str(conv.get("mode") or "round").lower()
        # 自由模式下单人在一场里最多尝试几次。上限是必需的：
        # A 问 B、B 反问 A 会自己绕圈。
        self.conv_max_per_agent: int = int(conv.get("max_per_agent") or 3)
        # 整轮看门狗（秒）。公式和理由见 core.convergence.watchdog_seconds：
        # turn_timeout 只管单次 prompt，管不了"一个 await 永不返回 → run()
        # 永不返回 → finally 永不执行 → 房间锁死"。实测卡过 16 分钟。
        wd = conv.get("watchdog_seconds")
        self.run_watchdog: int = convergence.watchdog_seconds(
            self.turn_timeout, int(wd) if wd else None)
        self.store.write_meta({
            "room": cfg.get("room", "main"),
            "cwd": self.cwd,
            "agents": [{"id": s.id, "name": s.name, "cmd": s.cmd} for s in self.specs.values()],
        })

        # 三个接缝。P1/P2/P3 各占一个，互不改对方的文件。
        self.opts = ConfigStore(root)                    # P1 — 模型/配置项
        sk_cfg = cfg.get("skills") or {}
        self.skills = Skills(                           # P3 — 技能
            roots=sk_cfg.get("roots"),
            enabled=sk_cfg.get("enabled", True),
        )
        d_cfg = cfg.get("discussion") or {}
        self.disc = Discussion(                         # P2 — 讨论协议
            self,
            method=d_cfg.get("method", "standard"),
            budget=Budget(
                max_rounds=int(d_cfg.get("max_rounds", 4)),
                max_tokens=int(d_cfg.get("max_tokens", 200_000)),
                max_seconds=int(d_cfg.get("max_seconds", 900)),
            ),
        )

        self._restore()

    def _restore(self) -> None:
        """把历史读回 transcript。

        重要细节：恢复的会话里 agent 自己没记忆（ACP session 是新的），所以 `seen` 全部归 0，
        但 `seen` 只控制「推什么给 agent」——让它们重新看到全部历史是正确的，否则接话会断裂。
        """
        rows = self.store.load()
        if not rows:
            return
        for r in rows:
            try:
                t = Turn(
                    seq=int(r.get("seq") or 0),
                    speaker=r.get("speaker") or "?",
                    name=r.get("name") or r.get("speaker") or "?",
                    text=r.get("text") or "",
                    ts=float(r.get("ts") or 0),
                    tokens=int(r.get("tokens") or 0),
                    status=r.get("status") or "ok",
                    ms=int(r.get("ms") or 0),
                    # 落盘时就盖好的两个戳，重启后必须原样读回来。丢了它们的话，
                    # 「这条是谁用哪个模型、哪次会话说的」在重启后就查不到了。
                    session=r.get("session") or "",
                    model=r.get("model") or "",
                )
            except (TypeError, ValueError):
                continue
            self.transcript.append(t)
            self.seq = max(self.seq, t.seq)
        log(f"恢复历史 {len(self.transcript)} 条（seq 到 {self.seq}）")

    # ---------- 广播 ----------

    async def broadcast(self, payload: dict) -> None:
        dead = []
        data = json.dumps(payload, ensure_ascii=False)
        for ws in self.sockets:
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    def snapshot(self, full_opts: bool = False) -> dict:
        """房间状态。

        full_opts=False（默认，广播/WS 用）：opts 只给当前值，不给 982 个备选。
        实测：全量 opts 是 128KB，而 push_state 每轮要推好几次——一场四轮讨论
        就白白抦几个 MB。模型列表前端需要时自己去 GET /api/options 拿一次就行。
        full_opts=True 保留旧行为，给兼容用。
        """
        return {
            "type": "state",
            "cwd": self.cwd,
            "order": self.order,
            "running": self.running,
            "turns": [t.__dict__ for t in self.transcript],
            "count": len(self.transcript),
            "opts": self.opts.snapshot() if full_opts else self.opts.brief(),
            "store": str(self.store.dir),
            "agents": [
                {
                    "id": s.id,
                    "name": s.name,
                    "color": s.color,
                    "model_hint": s.model_hint,
                    "connection": self.status.get(s.id, "未连接"),
                    "info": self.agents[s.id].info.get("agentInfo", {}) if s.id in self.agents else {},
                    # G2：会话是从哪来的 —— new 表示这个 agent 不记得上次聊过什么，
                    # resume/load 表示接回了原会话。界面上要能看出来。
                    "session": (self.agents[s.id].session_id or "") if s.id in self.agents else "",
                    "session_origin": (self.agents[s.id].session_origin or "") if s.id in self.agents else "",
                    # 上次会话没接回的原因原文（接回了 / 本来就没存过 = 空串）。
                    # 界面要能说「为什么」，不能只让人猜它是不是失忆了。
                    "resume_error": (getattr(self.agents[s.id], "resume_error", "") or "")
                    if s.id in self.agents else "",
                }
                for s in self.specs.values()
            ],
        }

    def session_of(self, speaker: str) -> str:
        """这条发言出自哪个 ACP 会话。人类没有会话，返回空串。"""
        ag = self.agents.get(speaker)
        return getattr(ag, "session_id", "") or ""

    def remember(self, topic: str, text: str, by: str = "",
                 initiator: str = "discussion") -> dict:
        """G1：把一场讨论的汇总写进房间记忆，并留一条审计。

        同步调用（`core/audit.py` 的 write 两边都支持），因为讨论引擎拿到的
        就是这个 Turn，攒着不写只会丢。

        initiator 用参数而不是读 self._initiator：`_one_turn` 在 finally 里把
        `_initiator` 还原成 "person"，汇总是在 one_turn 返回之后才写的，
        那时读到的永远是 person——这条留痕会撒谎。
        """
        row = self.memory.add(topic, text, by=by)
        if row:
            self.audit.write(
                agent=by or "room", turn_seq=self.seq, event="memory_write",
                tool="memory", title=(topic or "")[:80], status="ok", decision="allow",
                rule=f"讨论汇总写入房间记忆（现有 {len(self.memory.rows())} 条）",
                initiator=initiator,
                session=self.session_of(by),
            )
        return row

    async def promote_memory(self, agents: list[str] | None = None,
                             initiator: str = "person") -> list[dict]:
        """G6：把房间记忆里最近的结论推进每个 agent **自己的**长期记忆文件。

        和 G1 的区别不是"多写一份"，是活的时长：`core/memory.py` 那段是每轮
        随 prompt 发过去的，会话一换、进程一关就不在它脑子里了；写进它的
        instructions / memory store 才是"它记得"。生效时机也不同 ——
        **它的下一次会话**，不是现在这一场。审计和界面都按这个说法写。

        闸门是必需的，但两种发起人不能一视同仁：
          - `deny` 永远拦（`path_prefix: C:/Windows` 这类规则对人也不许绕）
          - `escalate` 的意思是"要有人看着"：人按按钮 = 有人看着；
            讨论引擎自动跑到这儿 = 没人看着，只能跳过并说明。
        所以 person 只过 deny，discussion 过完整四段。少写这一句，
        自动路径就会变成"配置里没写规则 → 默认 escalate → 每次静默不写"，
        而表现为「功能装了却没效果」。
        """
        rows = self.memory.recent(self.agent_memory.keep)
        if not rows:
            await self.announce("还没有讨论结论可以写进各 agent 的记忆")
            return []
        out: list[dict] = []
        for aid in (agents or self.agent_memory.agent_ids):
            tgt = self.agent_memory.target_for(aid)
            subj = PolicySubject(tool="agent_memory", agent=aid, initiator=initiator,
                                 path=str(tgt.path or ""), title=tgt.fmt)
            dec = self.policy.decide(subj)
            self.policy.record(dec)
            blocked = ""
            if dec.kind == "deny":
                blocked = f"策略拒绝：{dec.rule}"
            elif dec.kind == "escalate" and initiator != "person":
                blocked = f"没人确认，跳过：{dec.rule}"
            if blocked:
                one = {"agent": aid, "target": str(tgt.path or ""),
                       "status": "skipped", "detail": blocked, "backup": "", "chars": ""}
                self.audit.write(
                    agent=aid, turn_seq=self.seq, event="agent_memory", tool="agent_memory",
                    title=tgt.fmt, status="skipped", decision="deny",
                    rule=blocked, initiator=initiator, session=self.session_of(aid))
                out.append(one)
                continue
            res = self.agent_memory.promote(rows, agents=[aid])[0]
            res["decision"] = dec.kind
            self.audit.write(
                agent=aid, turn_seq=self.seq, event="agent_memory", tool="agent_memory",
                title=str(Path(res["target"]).name) if res["target"] else tgt.fmt,
                status="ok" if res["status"] in ("written", "unchanged") else res["status"],
                decision=dec.kind,
                rule=f"{res['detail']}"
                     + (f"（备份 {res['backup']}）" if res.get("backup") else ""),
                initiator=initiator, session=self.session_of(aid))
            out.append(res)
        await self.announce(f"写进各 agent 的记忆：{am_summarize(out)}"
                            "（生效时机是它们的下一次会话）")
        await self.push_state()
        return out

    def persist_sessions(self) -> None:
        """把当前 sessionId 落盘，供下次启动 resume/load。

        只存「这次真的开过口」的会话。原因不是洁癖，是实测：codex 的 rollout 文件
        要等第一轮跑完才落盘，一个从没发言的 session 去 resume 必然报
        `no rollout found for thread id`；而旧逻辑会顺手把存着的**旧** id 覆盖掉，
        于是「本来还能接回的那次会话」被一个空壳替换，永久丢了。
        没开口的会话没什么可丢，留着旧 id 让下次再试一次才是对的。

        顺带做一次「id 变了没」的判定：变了就写一条 session_switch 审计。
        这条是给用户看的——agent 换了 sessionId 等于上下文清零，而界面上一句话
        可能说得照样顺，只有审计能告诉你「刚才那轮它其实是失忆后重新起的」。
        """
        cur = {aid: ag.session_id for aid, ag in self.agents.items()
               if ag.session_id and aid in self.spoke}
        if not cur:
            return
        merged = dict(self.saved_sessions)
        for aid, sid in cur.items():
            prev = self.saved_sessions.get(aid)
            # prev 为空是首次建会话，不算"变了"，否则每次启动刷五条噪音。
            if prev and prev != sid:
                self.audit.write(
                    agent=aid, turn_seq=self.seq, event="session_switch",
                    tool="session", title=f"{prev} → {sid}", status="ok",
                    decision="allow",
                    rule="sessionId 变了：agent 侧换了新会话，之前的上下文不在它脑子里了",
                    initiator=getattr(self, "_initiator", "person"),
                    session=sid,
                )
            merged[aid] = sid
        self.saved_sessions = merged
        self.store.save_sessions(merged)

    async def push_state(self) -> None:
        await self.broadcast(self.snapshot())

    # ---------- ACP 回调 ----------

    async def _on_update(self, evt: dict) -> None:
        aid = evt["agent"]
        update = (evt.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            txt = text_of_update(update)
            if txt:
                self.streaming[aid] = self.streaming.get(aid, "") + txt
                await self.broadcast({"type": "chunk", "agent": aid, "text": txt})
        elif kind == "tool_call":
            # P6 修复：tool_call 携带 kind/title，要记住。ACP 的 tool_call_update
            # 往往只带 toolCallId + status，不带 kind/title——不记就会在审计里
            # 变成 tool=unknown / title=""，结果无法对回哪次调用（实测就是如此）。
            tcid = update.get("toolCallId")
            tkind = update.get("kind")
            ttitle = update.get("title")
            if tcid:
                self.inflight_tools[tcid] = {"tool": tkind, "title": ttitle}
            await self.broadcast({
                "type": "tool",
                "agent": aid,
                "title": ttitle or tkind or "tool",
                "status": update.get("status"),
            })
            # 策略闸门真正生效的地方。
            # 实测（P13）：codex/claude/pi/omp 写文件时**不发 session/request_permission**，
            # 自己就把工具跑了。所以只挂 _on_permission 等于没挂——这一行才是每一次
            # 工具调用都会经过的路径。判定 + 审计在这里落，事后可查、可计数。
            subj = PolicySubject(tool=str(tkind or ""), title=str(ttitle or ""),
                                 agent=aid, initiator=self._initiator)
            dec = self.policy.decide(subj)
            self.policy.record(dec)
            if dec.kind != "allow" and tcid:
                # 记下来，供 tool_call_update 回填同一判定（结果行要和决定行对上）
                self.inflight_tools.setdefault(tcid, {})["decision"] = dec
            await self.audit.write(agent=aid, turn_seq=self.seq, event="tool_call", tool=tkind, title=ttitle, status="pending", decision=dec.kind, rule=dec.rule, initiator=self._initiator, toolCallId=tcid)
        elif kind == "tool_call_update":
            # 从发起时记住的字段补齐 kind/title（update 里通常没有）
            tcid = update.get("toolCallId")
            known = self.inflight_tools.get(tcid) or {}
            tkind = update.get("kind") or known.get("tool")
            ttitle = update.get("title") or known.get("title")
            await self.broadcast({
                "type": "tool",
                "agent": aid,
                "title": ttitle or "tool",
                "status": update.get("status"),
            })
            dec = known.get("decision")
            if dec is not None:
                dkind, drule = dec.kind, dec.rule
            else:
                # 没有 initiator 的情况（比如进程重启后收到孤儿 update）：重新判一次
                subj2 = PolicySubject(tool=str(tkind or ""), title=str(ttitle or ""),
                                      agent=aid, initiator=self._initiator)
                dec = self.policy.decide(subj2)
                dkind, drule = dec.kind, dec.rule
            await self.audit.write(agent=aid, turn_seq=self.seq, event="tool_result", tool=tkind, title=ttitle, status=update.get("status"), decision=dkind, rule=drule, initiator=self._initiator, toolCallId=tcid)

    async def _on_permission(self, evt: dict) -> dict:
        """ACP session/request_permission 的宿主侧处理。

        P10 之后这里重写成了**策略闸门**（core/policy.py），不再是
        “permission == allow 就放行”的常量判断。顺序是硬性的：

            判定 -> 写审计 -> 才动手

        先写审计再回复 agent：进程若在中间挂掉，会留下“有审计无动作”，
        而不是“有动作无审计”。后者查不出来，前者一眼可见。

        initiator 取自 self._initiator：人在房间里打字是 person，
        讨论引擎自动跑出来的是 discussion。它决定审计里那行是
        “有人看着”还是“没人看着”。
        """
        aid = evt["agent"]
        params = evt["params"]
        options = params.get("options") or []
        mode = self.specs[aid].permission          # 老字段：allow / deny / ask
        initiator = getattr(self, "_initiator", "person")
        title = params.get("toolCall", {}).get("title") or "工具调用"

        def pick(kinds: tuple[str, ...]) -> dict | None:
            for o in options:
                if o.get("kind") in kinds:
                    return o
            return None

        # ---- 1. 判定 ----
        subj = PolicySubject.from_permission(params, agent=aid, initiator=initiator)
        decision = self.policy.decide(subj)

        # 老的 permission: "deny" 仍然算数，但它只能让结果更严，
        # 不能把策略已经判的 deny 变回 allow。
        if mode == "deny" and decision.kind != "deny":
            decision = PolicyDecision(
                "deny",
                f"config.agents[{aid}].permission == deny（覆盖策略：{decision.rule}）",
                rule_source="mode_deny", matched=subj.as_dict(),
            )

        self.policy.record(decision)

        # ---- 2. 写审计（先于任何动作） ----
        await self.audit.write(
            agent=aid, turn_seq=self.seq, event="permission",
            tool=subj.tool or "unknown", title=title, status="pending",
            decision=decision.kind, rule=decision.rule, initiator=initiator,
        )

        # ---- 3. 按判定动手 ----
        if decision.kind == "allow":
            chosen = pick(("allow_always", "allow_once")) or (options[0] if options else None)
            if not chosen:
                log(f"[{aid}] 策略放行但 agent 没给 allow 选项，只能 cancelled")
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": chosen["optionId"]}}

        if decision.kind == "deny":
            denied = pick(("reject_once", "reject_always"))
            if denied:
                return {"outcome": {"outcome": "selected", "optionId": denied["optionId"]}}
            # agent 没给 reject 选项时只能 cancel；审计里已经记了 deny，
            # 所以“平台拒了”仍然可查，不会变成“agent 自己拒绝了”。
            log(f"[{aid}] 策略拒绝，但 agent 未提供 reject 选项，回 cancelled")
            return {"outcome": {"outcome": "cancelled"}}

        # escalate：交给人。前端 P9 卡片接这个。
        self.perm_seq += 1
        pid = f"perm{self.perm_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.perm_waiters[pid] = fut
        await self.broadcast({
            "type": "permission",
            "id": pid,
            "agent": aid,
            # P9：前端靠 turn_seq 把审批卡片挂到正确的那一轮（内联而不是模态）
            "turn_seq": self.seq,
            "tool": title,
            "why": decision.rule,
            "options": [{"optionId": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
                        for o in options],
        })
        try:
            option_id = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            log(f"[{aid}] 权限请求超时，自动拒绝")
            await self.audit.write(
                pid=pid, agent=aid, turn_seq=self.seq, event="permission",
                tool=subj.tool or "unknown", title=title, status="error",
                decision="deny", rule="escalate 超时（600s 无人应答）",
                initiator=initiator,
            )
            return {"outcome": {"outcome": "cancelled"}}
        finally:
            self.perm_waiters.pop(pid, None)
        if option_id == "__cancel__":
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    def resolve_permission(self, pid: str, option_id: str) -> bool:
        fut = self.perm_waiters.get(pid)
        if not fut or fut.done():
            return False
        fut.set_result(option_id)
        self.audit.write(pid=pid, event="decision", tool="permission", title="权限决定", status="ok", user_option=option_id, initiator="person")
        return True

    # ---------- 发言 ----------

    def _prompt_for(self, aid: str, note: str) -> str:
        """只推「上次发言之后别人说了什么」。agent 自己的上下文在 ACP session 里。

        组装逻辑已抽到 `core/context.py`（纯函数，可单测）。这里只负责三件
        与宿主状态有关的事：读游标、推进游标、拿技能块。
        """
        start = self.seen.get(aid, 0)
        pending = ctx.pending_since(self.transcript, start, aid)
        self.seen[aid] = len(self.transcript)

        plan = ctx.build_body(pending, note)
        self._last_plan = plan          # 给审计用（本轮压缩了几条）
        head = self._skill_head(aid)
        imgs = [a for t in pending for a in (t.attachments or [])]
        spec = self.specs[aid]
        # 角色文本要一起传：只给名字的话，四个 agent 说话还是一个腔调
        # （"独立意识"那半个需求靠的就是这三个字段）。
        roles = {a: (self.specs[a].role, self.specs[a].tag)
                 for a in self.order if a in self.specs}
        return ctx.assemble(PREAMBLE, head, plan, note=note,
                            wait_hint=WAIT_HINT, attachments=imgs,
                            self_name=spec.name, self_id=aid,
                            roster=[(a, self.specs[a].name, self.specs[a].tag)
                                    for a in self.order if a in self.specs],
                            roles=roles,
                            self_role=spec.role, self_tag=spec.tag,
                            addressed=aid in self._addressed,
                            memory=self.memory.block())

    def _pending_images(self, aid: str) -> list[dict]:
        """这个 agent 自上次发言以来、还没看过的图片元数据。

        和 _prompt_for 用同一个游标（self.seen），所以文字和图片永远同步：
        不会出现"文字推了但图没推"或者反过来。
        注意 _prompt_for 会推进 seen，所以必须在它**之前**调用。
        """
        start = self.seen.get(aid, 0)
        return [a for t in ctx.pending_since(self.transcript, start, aid)
                for a in (t.attachments or [])]

    def _skill_head(self, aid: str) -> str:
        """P3 接缝：把该 agent 的「技能清单 + 排队待注入正文」拼到 prompt 前面。

        清单每轮都给（发现机制）；正文只在 agent 点名 `[USE_SKILL: 名字]` 之后给一次。
        `note` 只在 round 0 非空，之后 ranking 就没信号了，所以 query 取最近一条人类发言。
        """
        sk = self.skills
        if not sk.enabled:
            return ""
        try:
            topic = ctx.pick_topic(self.transcript)
            cat = sk.catalog_prompt(agent_id=aid, query=topic)
            inj = sk.injection_for(aid)          # 取走即清空，放最后执行
        except Exception as exc:  # noqa: BLE001 —— 技能块绝不能让房间停下来
            log(f"[skills] {aid} 组装技能块失败: {type(exc).__name__}: {exc}")
            return ""
        parts = [p for p in (cat, inj) if p]
        return ("\n\n".join(parts) + "\n\n") if parts else ""

    # ---------- 单个 agent 的一次发言 ----------

    async def _prepare(self, aid: str, note: str) -> tuple[str, list[dict]]:
        """算出这个 agent 这轮要看什么：返回 (prompt, images)。

        顺序有讲究：**先取图再生成 prompt**。`_prompt_for` 会把 self.seen 推进到
        当前 transcript 末尾，之后再取图就永远拿到空列表——图会静默丢失。
        在并行轮里所有 agent 的 prompt 都会先算好再一起跑，所以这个顺序必须对。
        """
        agent = self.agents[aid]
        spec = self.specs[aid]
        self.status[aid] = "发言中"
        self.streaming[aid] = ""
        await self.broadcast({"type": "turn_start", "agent": aid, "name": spec.name})
        pending_imgs = self._pending_images(aid)
        prompt = self._prompt_for(aid, note)
        images = self.attachments.to_acp_blocks(pending_imgs) if pending_imgs else []
        if images:
            log(f"[{aid}] 本轮附带 {len(images)} 张图片")
        return prompt, images

    async def _execute(self, aid: str, prompt: str, initiator: str = "person",
                       images: list[dict] | None = None) -> Turn:
        """跑一轮，返回 Turn。不写进 transcript（由调用方决定顺序）。

        initiator 在**整个 run 期间**生效，因为权限请求是异步到达的，
        可能在 prompt 返回之后才被处理。用 try/finally 还原，避免泄漏到下一轮。
        """
        prev_initiator = getattr(self, "_initiator", "person")
        self._initiator = initiator
        agent = self.agents[aid]
        spec = self.specs[aid]
        t0 = time.time()
        wait_retried = False          # 被点名时的强制追问只做一次
        try:
            res = await agent.prompt(prompt, timeout=self.turn_timeout, images=images)
            # 这一枪打出去了 = agent 侧这个会话从此有内容可恢复（见 self.spoke 注释）。
            self.spoke.add(aid)
            text = clean_text(self.streaming.get(aid, "").strip())
            # wait：agent 明确表示这轮没话说。不落 transcript（不占别人的上下文），
            # 但审计留一行，这样"它为什么没说话"是可查的，而不是看起来像故障。
            _why = ctx.wait_reason(text, _WAIT_RE)
            # 被点名的人不许退场。实测的原始 bug 就长这样：
            #   grant  omp  named: @omp
            #   wait   omp  「这条点名 @omp，不是我。」
            # 身份头已经能防住大部分，但身份只是提示词，模型仍可能不照做。
            # 所以这里再加一道：被点名却想 wait，就**直接追问一次**。
            # 只重试一次（不是循环）：第二次还不说，说明它真的没内容，
            # 再逼下去只会让它编。
            if _why is not None and aid in self._addressed and not wait_retried:
                wait_retried = True
                log(f"[{aid}] 被点名却说 wait（{_why}），强制追问一次")
                await self.audit.write(
                    agent=aid, turn_seq=self.seq, event="turn_wait_retry",
                    tool="wait", title=_why, status="ok", decision="allow",
                    rule="被点名的 agent 不许退场，已追加一次直接追问",
                    initiator=initiator, session=agent.session_id or "",
                )
                self.streaming[aid] = ""
                res = await agent.prompt(
                    "这一轮人类点名要你回答，你刚才回了「没有内容可说」。"
                    "现在直接给出你的回答：用你自己的判断说一句实质内容，"
                    "不要再说没有内容，不要解释你为什么之前没说。",
                    timeout=self.turn_timeout, images=images)
                text = clean_text(self.streaming.get(aid, "").strip())
                _why = ctx.wait_reason(text, _WAIT_RE)

            if _why is not None:
                why = _why
                log(f"[{aid}] 本轮 wait：{why}")
                await self.audit.write(
                    agent=aid, turn_seq=self.seq, event="turn_wait",
                    tool="wait", title=why, status="ok", decision="allow",
                    rule=("被点名但两次都拒绝回答" if aid in self._addressed
                          else "agent 主动表示本轮无新增内容"),
                    initiator=initiator, session=agent.session_id or "",
                )
                self.status[aid] = "在线"
                return Turn(seq=0, speaker=aid, name=spec.name, text="", ts=time.time(),
                            status="wait", ms=int((time.time() - t0) * 1000))
            if not text:
                # 空正文 ≠ 说话。以前这里填一句占位文本就当 ok 发出去，转录里
                # 长得和真回答一样，翻记录根本看不出「它其实一个字没说」——
                # 实测 pi 因为网关 401 连续两轮空正文，全被记成 ok。
                # 现在标成 empty：不占别人的上下文，但审计里可查、界面能区分。
                log(f"[{aid}] 空正文（stopReason={res.get('stopReason')}），记为 empty")
                await self.audit.write(
                    agent=aid, turn_seq=self.seq, event="turn_empty",
                    tool="prompt", title=str(res.get("stopReason") or ""),
                    status="empty", decision="allow",
                    rule="模型返回空正文（没说话，也没声明 [WAIT]）",
                    initiator=initiator, session=agent.session_id or "",
                )
                self.status[aid] = "在线"
                return Turn(seq=0, speaker=aid, name=spec.name, text="", ts=time.time(),
                            status="empty", ms=int((time.time() - t0) * 1000))
            # 回复守卫：错误文本 / traceback / tool-call 原文都不是发言。
            # 它们写进转录之后跟正常回答长得一样，还会被**别的 agent 当成内容引用**
            # ——实测 claude 和 pi 真的围绕 omp 那条 401 讨论了好几轮。
            # 拦下来记审计 + 广播一条 notice，让用户知道"它为什么没说话"。
            verdict = reply_guard.check(
                text, stop_reason=str(res.get("stopReason") or ""))
            if verdict.blocked:
                log(f"[{aid}] 回复被拦下（{verdict.kind}）：{verdict.reason}")
                await self.audit.write(
                    agent=aid, turn_seq=self.seq, event="turn_blocked",
                    tool="guard", title=verdict.kind, status="blocked",
                    decision="deny",
                    # 原文摘录必须留：被拦下的不进转录，不留原文的话
                    # **误杀就永远无法复核**。脱敏后再写，防止二次泄漏。
                    rule=f"{verdict.reason}；原文：{reply_guard.redact(text)}",
                    initiator=initiator, session=agent.session_id or "",
                )
                await self.broadcast({
                    "type": "notice", "level": "warn", "agent": aid,
                    "text": f"{spec.name} 这轮被拦下（{verdict.kind}）：{verdict.hint}",
                    "ts": time.time(),
                })
                self.status[aid] = "在线"
                return Turn(seq=0, speaker=aid, name=spec.name, text="", ts=time.time(),
                            status="blocked", ms=int((time.time() - t0) * 1000))
            usage = res.get("usage") or {}
            turn = Turn(seq=0, speaker=aid, name=spec.name, text=text, ts=time.time(),
                        tokens=int(usage.get("totalTokens") or 0),
                        ms=int((time.time() - t0) * 1000))
            self.status[aid] = "在线"
            # 上下文压缩统计。只在**真的压缩过**时写一行——没压缩就写等于噪音。
            plan = getattr(self, "_last_plan", None)
            if plan is not None and (plan.compressed or plan.truncated):
                await self.audit.write(
                    agent=aid, turn_seq=self.seq, event="context_plan",
                    tool="context", title=json.dumps(plan.as_dict(), ensure_ascii=False),
                    status="ok", decision="allow",
                    rule=f"近 {ctx.NEAR} 条全文，更早压到 {ctx.COMPRESS} 字",
                    initiator=initiator,
                )
                self._last_plan = None
        except AcpError as exc:
            turn = Turn(seq=0, speaker=aid, name=spec.name,
                        text=f"⚠ ACP 错误：{exc}", ts=time.time(), status="error")
            self.status[aid] = "出错"
        except asyncio.TimeoutError:
            turn = Turn(seq=0, speaker=aid, name=spec.name,
                        text=f"⚠ 超时（{self.turn_timeout}s），已中断", ts=time.time(),
                        status="cancelled")
            self.status[aid] = "超时"
            await agent.cancel()
        except Exception as exc:  # noqa: BLE001
            turn = Turn(seq=0, speaker=aid, name=spec.name,
                        text=f"⚠ {type(exc).__name__}: {exc}", ts=time.time(), status="error")
            self.status[aid] = "出错"
        finally:
            # 还原，避免 initiator 泄漏到下一轮（下一轮可能是人类打字）
            self._initiator = prev_initiator
            # agent 进程如果在本轮死过又重启，session_id 已经换了；
            # 每轮结束都落一次盘（一个小 JSON），下次启动才接得上。
            self.persist_sessions()
        return turn

    async def _commit(self, turn: Turn) -> None:
        if turn.status in ("wait", "empty", "blocked"):
            # 这三种都不占 transcript 序号，也不广播成一条发言——它们没有内容。
            # wait 是主动选择不说；empty 是模型没吐字；blocked 是吐出来的
            # 根本不是话（网关错误 / traceback / tool-call 原文）。
            # 区别只在审计和界面上怎么标。
            await self.push_state()
            return
        self.seq += 1
        turn.seq = self.seq
        turn.session = self.session_of(turn.speaker)
        turn.model = self.opts.model_of(turn.speaker)
        self.transcript.append(turn)
        self.streaming.pop(turn.speaker, None)
        self.store.append(turn)
        await self.broadcast({"type": "turn_end", "turn": turn.__dict__})
        # P3 接缝：agent 本轮点名的技能，正文排进它下一轮的 prompt 前面。
        # 只在 agent 发言时触发（say_human 不调 _commit），所以不会给人类排队。
        if turn.speaker in self.agents:
            names = self.skills.wants(turn.text, agent_id=turn.speaker)
            if names:
                self.skills.queue_injection(turn.speaker, names)

    async def _one_turn(self, aid: str, note: str, initiator: str = "person") -> Turn:
        prompt, images = await self._prepare(aid, note)
        turn = await self._execute(aid, prompt, initiator=initiator, images=images)
        await self._commit(turn)
        await self.push_state()
        return turn

    async def _parallel_round(self, ids: list[str], note: str,
                              initiator: str = "person") -> list[Turn]:
        """一轮内所有 agent 同时跑，回来后按 order 顺序落盘。"""
        prompts: dict[str, str] = {}
        imgs: dict[str, list[dict]] = {}
        for aid in ids:
            prompts[aid], imgs[aid] = await self._prepare(aid, note)
        await self.push_state()
        t0 = time.time()
        results = await asyncio.gather(
            *[self._execute(aid, prompts[aid], initiator=initiator, images=imgs[aid])
              for aid in ids],
            return_exceptions=True
        )
        log(f"并行轮完成，最慢 {time.time() - t0:.1f}s（{len(ids)} 个 agent）")
        out: list[Turn] = []
        for aid, res in zip(ids, results):
            if isinstance(res, BaseException):
                turn = Turn(seq=0, speaker=aid, name=self.specs[aid].name,
                            text=f"⚠ {type(res).__name__}: {res}", ts=time.time(), status="error")
            else:
                turn = res
            await self._commit(turn)
            out.append(turn)
        await self.push_state()
        return out

    # ---------- 给 Discussion / 外部模块用的公开接口 ----------
    # 约定：P2 只能调这几个，不要直接碰内部 _xxx。

    async def announce(self, text: str) -> None:
        """系统提示（阶段切换、停因）。不算发言，不进 transcript。"""
        await self.broadcast({"type": "notice", "text": text, "ts": time.time()})

    async def one_turn(self, aid: str, note: str,
                       initiator: str = "person") -> Turn:
        """讨论引擎用的公开入口。initiator 必须透传：
        `core/discussion.py` 每处都按 `initiator="discussion"` 调用，
        这个参数一旦被剥掉，整场讨论会在第一个阶段抛 TypeError，
        而它发生在 asyncio 任务里，界面上只表现为「点了讨论，什么都没发生」。
        """
        return await self._one_turn(aid, note, initiator=initiator)

    async def parallel_round(self, ids: list[str], note: str,
                             initiator: str = "person") -> list[Turn]:
        return await self._parallel_round(ids, note, initiator=initiator)

    # ---------- 运行期增删 agent ----------

    def _spec_from_cfg(self, a: dict) -> "AgentConfig":
        return AgentConfig(
            id=a["id"],
            name=a.get("name", a["id"]),
            cmd=list(a["cmd"]),
            # 每个 agent 可以覆盖 cwd；同样按仓库根解析，保持和顶层一致
            cwd=self._resolve_cwd(a.get("cwd") or self.cwd, self.root),
            color=a.get("color", "#888888"),
            permission=a.get("permission", "ask"),
            model_hint=a.get("model_hint", ""),
            env=a.get("env") or {},
            role=a.get("role", ""),
            tag=a.get("tag", ""),
            style=a.get("style", ""),
        )

    async def add_agent(self, cfg_agent: dict) -> str:
        """把一个已经校验过的 agent 配置接进房间，立刻可用（不用重启）。

        三种情况：
          - 新 id：注册、追加到 order 末尾、seen 从当前 transcript 末尾开始
            （**不回放历史**：新 agent 不该替它没在场的讨论发言）
          - 同 id 但命令变了：关掉旧实例，按新配置重建
          - 同 id 且命令一样：什么都不做，避免把正在聊的 session 掐掉
        """
        aid = cfg_agent["id"]
        spec = self._spec_from_cfg(cfg_agent)
        old = self.specs.get(aid)
        if old is not None:
            if old.cmd == spec.cmd and old.name == spec.name and old.permission == spec.permission:
                return "unchanged"
            # 命令变了：先关旧实例
            agent = self.agents.get(aid)
            if agent is not None:
                try:
                    await agent.stop()
                except Exception as exc:  # noqa: BLE001
                    log(f"[{aid}] 关旧实例失败（继续重建）：{exc}")
            self.specs[aid] = spec
            self.agents[aid] = AcpAgent(spec, self._on_update, self._on_permission)
            self.status[aid] = "连接中…"
            log(f"[{aid}] 已按新命令重建，开始连接…")
            await self.push_state()
            asyncio.create_task(self.start_agent(aid))
            return "rebuilt"

        self.specs[aid] = spec
        self.agents[aid] = AcpAgent(spec, self._on_update, self._on_permission)
        self.order.append(aid)
        self.seen[aid] = len(self.transcript)
        self.status[aid] = "连接中…"
        log(f"[{aid}] 已接入房间（{len(self.order)} 个 agent），开始连接…")
        await self.push_state()
        # 后台起进程：npx 首次拉包可能几十秒，不能把 HTTP 请求卡在那里。
        # 状态变化会经由 push_state 推到界面，用户能看到「连接中… → 在线」。
        asyncio.create_task(self.start_agent(aid))
        return "added"

    async def remove_agent(self, aid: str) -> bool:
        """把 agent 摘出房间并关掉进程。已有发言保留在 transcript 里。"""
        if aid not in self.agents:
            return False
        agent = self.agents.pop(aid)
        self.specs.pop(aid, None)
        self.seen.pop(aid, None)
        self.status.pop(aid, None)
        self.streaming.pop(aid, None)
        if aid in self.order:
            self.order.remove(aid)
        try:
            await agent.stop()
        except Exception as exc:  # noqa: BLE001
            log(f"[{aid}] 关进程失败：{exc}")
        log(f"[{aid}] 已离开房间（剩 {len(self.order)} 个 agent）")
        await self.push_state()
        return True

    async def say_human(self, text: str, attachments: list[dict] | None = None) -> Turn:
        """人类发言。attachments 是已落盘的元数据（不是 base64）。

        图片挂在**这一条人类发言上**：下一轮每个 agent 会把它和正文一起收进
        session/prompt 的 image 内容块。图只发一次，不用每轮重发。
        """
        self.seq += 1
        turn = Turn(seq=self.seq, speaker="human", name="你", text=text, ts=time.time(),
                    attachments=list(attachments or []))
        self.transcript.append(turn)
        self.store.append(turn)
        await self.broadcast({"type": "turn_end", "turn": turn.__dict__})
        return turn

    def _activity(self) -> dict[str, int]:
        """最近 30 条里每个 agent 说了几次。仲裁补位时用（纯函数在 core/context）。"""
        return ctx.activity(self.transcript, list(self.agents))

    def _last_human_text(self) -> str:
        return ctx.last_human_text(self.transcript)

    async def _free_session(self, *, note: str, initiator: str,
                            rounds: int, opening_ids: list[str]) -> None:
        """自由发言：顺序由对话决定，一人可说多次，也可以不说。

        ## 和 _parallel_round 的根本区别

        `_parallel_round` 先把所有人的 prompt 算好再一起跑 —— 所以同一轮内
        A 看不到 B 刚说的，只能对着上一轮的旧上下文发言。表现出来就是
        "四个人轮流总结同一个话题"，而不是"接着某人的话往下说"。

        这里**串行**：每说完一句就落盘，下一个人取 prompt 时自然看得到它
        （`_prompt_for` 推的是"我上次发言之后别人说的"，游标机制不用改）。
        于是"接着反驳"成为可能。

        ## 顺序规则

            上一句 @ 了谁 → 谁接话（且被点名者必须回答）
            否则        → 说话最少的人补位
            到上限      → 结束

        规则在 `core/turntaking.py`，纯函数、可测、零额外模型调用。

        ## 为什么计数用 counts 而不是只记"说过几次"

        被点名的人若回 `[WAIT]`（它确实可能没什么可说），只统计"说过几次"的话
        它会被反复选中、绕不出来。所以**每次尝试都计数**（含 wait），
        `max_per_agent` 限的是尝试次数。
        """
        roster = [(a, self.specs[a].name) for a in self.order if a in self.specs]
        if not roster:
            return
        # attempts：被叫到发言权的次数（含 wait），用于防死循环
        # spoke   ：真的说过话的次数（wait 不算），用于判断"谁还没开过场"
        # 两个分开是必需的：被点名的人若回了 wait，spoke 不加，于是它仍算"没开过场"，
        # 但 attempts 会涨，不会被无限选中。
        attempts: dict[str, int] = {a: 0 for a, _ in roster}
        spoke: dict[str, int] = {a: 0 for a, _ in roster}
        # 轮到过但没说出内容（空正文/故障）。和 spoke 分开记——
        # 补位只该让"还没轮到的人"有机会，不该让故障的人一直占着补位名额。
        tried: dict[str, int] = {a: 0 for a, _ in roster}
        budget = tt.budget_steps(len(roster), rounds, self.conv_max_per_agent)
        log(f"[自由发言] 开始：{len(roster)} 人，上限 {budget} 步，"
            f"单人上限 {self.conv_max_per_agent} 次")

        step = None
        if opening_ids:
            step = tt.Step(opening_ids[0], "addressed", "人类这一句点名由它开场")
        if step is None:
            step = tt.opening(roster=roster, activity=self._activity())

        pick_history: list[str] = []
        for i in range(budget):
            if not self.running or step is None:
                break
            aid = step.speaker
            if aid not in self.agents:
                break
            attempts[aid] = attempts.get(aid, 0) + 1
            pick_history.append(step.reason)
            # 被点名者必须回答：复用已有的"强制追问一次"逻辑
            self._addressed = {aid} if step.reason == "addressed" else set()
            await self.audit.write(
                agent=aid, turn_seq=self.seq, event="turn_pick",
                tool="turntaking", title=aid, status="ok", decision="allow",
                rule=f"{step.reason}: {step.detail}",
                initiator=initiator,
            )
            log(f"[自由发言] 第 {i + 1}/{budget} 步 → {aid}（{step.reason}：{step.detail}）")
            turn = await self._one_turn(aid, note if i == 0 else "",
                                        initiator=initiator)
            if turn.status in ("wait", "empty", "blocked"):
                # 没说话就不算"说过"，也不拿空正文去解析下一个发言人。
                last = ""
                if turn.status in ("empty", "blocked"):
                    # 但"轮到过却没说"要记下来：否则一个故障 agent
                    # 会永远留在补位候选里被反复挑中。
                    # blocked 也算——模型吐的是错误，不是内容，下一句不该接它。
                    tried[aid] = tried.get(aid, 0) + 1
            else:
                spoke[aid] = spoke.get(aid, 0) + 1
                last = turn.text or ""
            step = tt.next_speaker(
                last_text=last, last_speaker=aid, roster=roster,
                spoke=spoke, attempts=attempts,
                max_per_agent=self.conv_max_per_agent, tried=tried)
            # 连续一整轮没人接别人的话 → 话题走平，收。
            # 不加这条的话补位规则会让循环走满 budget，那是"换了顺序的轮询"，
            # 不是自由发言。实测 4 人 rounds=3 会一路走满 12 步。
            if step is not None and tt.went_flat(pick_history, roster):
                log(f"[自由发言] 连续 {len(roster)} 步没人接话，话题走平，提前结束")
                await self.audit.write(
                    agent="arbiter", turn_seq=self.seq, event="turn_flat",
                    tool="turntaking", title="话题走平",
                    status="ok", decision="allow",
                    rule=f"连续 {len(roster)} 步都是补位、无人接话",
                    initiator=initiator,
                )
                break
        self._addressed = set()
        log(f"[自由发言] 结束：发言 {sum(spoke.values())} 次 / 尝试 {sum(attempts.values())} 次，"
            f"各人说过 {spoke}")

    async def run(self, speakers: list[str] | None = None, rounds: int = 1, note: str = "") -> None:
        """跑若干轮。speakers 为空则交给发言权仲裁决定谁开口。

        **这是本平台最省 token 的一处改动。** 原来 `speakers or self.order` 意味着
        一句闲聊叫醒全房间：实测 7.4 万 token、4 分 20 秒，OMP 一家占 95%。
        现在让 `core/arbitration.py` 按「点名 > 回复对象 > 职责命中 > 活跃度补位」
        决定，规则是确定性的、可解释的、能进审计的。

        调用方明确给了 speakers（前端 @ 点名、/api/run 指定）就以它为准，
        仲裁不覆盖人的决定。
        """
        self.running = True
        await self.push_state()
        if speakers is None:
            grant = arbitrate(
                text=self._last_human_text(),
                explicit=None,
                roster=[(a, self.specs[a].name) for a in self.order if a in self.specs],
                activity=self._activity(),
            )
            ids = [a for a in grant.speakers if a in self.agents]
            # 点名/回复/显式指定 → 这几个人不许用退出口（他们是被叫的）
            self._addressed = set(ids) if grant.reason in ("named", "reply_to", "explicit") else set()
            log(f"[仲裁] {grant.reason}：{ids} —— {grant.detail}"
                + (f"（其中 {sorted(self._addressed)} 被点名，必须回答）" if self._addressed else ""))
            await self.audit.write(
                agent="arbiter", turn_seq=self.seq, event="turn_grant",
                tool="arbitration", title=", ".join(ids) or "(无人)",
                status="ok", decision="allow",
                rule=f"{grant.reason}: {grant.detail}",
                initiator=getattr(self, "_initiator", "person"),
            )
        else:
            ids = [a for a in speakers if a in self.agents]
        if not ids:
            log("[仲裁] 本轮没有人被授权发言，跳过")
            self.running = False
            await self.push_state()
            return
        # 自由发言也要走同一个 finally。原来这里直接 `return`，绕过了下面的
        # `self.running = False` —— 实测症状是 `/api/state` 永远报 running=true，
        # 于是 `/api/say` 一律 500「已经在跑了一轮」，而日志里那一场早就
        # 「[自由发言] 结束」了。房间从此只能重启才能再用。
        try:
            if self.conv_mode == "free":
                await self._free_session(
                    note=note, initiator=getattr(self, "_initiator", "person"),
                    rounds=rounds, opening_ids=list(ids))
                return
            for r in range(rounds):
                if not self.running:
                    return
                round_note = note if r == 0 else ""
                if self.parallel and len(ids) > 1:
                    await self._parallel_round(ids, round_note)
                else:
                    for aid in ids:
                        if not self.running:
                            return
                        await self._one_turn(aid, round_note)
        finally:
            self.running = False
            self._addressed = set()
            for aid in self.agents:
                if self.status.get(aid) == "发言中":
                    self.status[aid] = "在线"
            await self.push_state()

    def start_background(self, speakers: list[str] | None = None, rounds: int = 1,
                         note: str = "") -> None:
        if self.running:
            raise RuntimeError("已经在跑了一轮")
        self.task = asyncio.create_task(self._guarded_run(speakers, rounds, note))

    async def _guarded_run(self, speakers: list[str] | None, rounds: int,
                           note: str) -> None:
        """跑一轮，并且**保证**不管里面怎么挂，running 都会被放开。

        上面那个 finally 修的是"正常返回时漏放"。这一层修的是另一种情况：
        某个 await 真的卡死（ACP 不回包、agent 进程僵住），run() 永远不返回，
        finally 也就永远不执行——房间同样锁死，只能重启。

        看门狗按"整轮预算"给宽限：turn_timeout 是**单轮**上限，一轮里有
        4 个 agent × 若干步，所以上限 = turn_timeout × 步数余量，再乘 2。
        正常跑不完这么多；真到了这个点，说明有东西僵住了，这时候放开房间
        比继续等更对——用户至少能停、能重来。
        """
        watchdog = getattr(self, "run_watchdog", 0) or 0
        try:
            if watchdog > 0:
                await asyncio.wait_for(self.run(speakers, rounds, note),
                                       timeout=watchdog)
            else:
                await self.run(speakers, rounds, note)
        except asyncio.TimeoutError:
            log(f"[看门狗] 本轮超过 {watchdog}s 仍未结束，判定为僵住，强制放开房间")
            await self.audit.write(
                agent="arbiter", turn_seq=self.seq, event="run_watchdog",
                tool="watchdog", title=f"{watchdog}s", status="error",
                decision="allow", rule="整轮超出上限，已强制结束并放开 running",
            )
        except asyncio.CancelledError:
            # 用户点了停止。run() 的 finally 已经放开 running，这里不吞异常。
            self.running = False
            raise
        except Exception as exc:  # noqa: BLE001
            # run() 自己会记 turn 级的错误；到这里说明是它**之外**的崩塌。
            log(f"[看门狗] 本轮异常退出：{type(exc).__name__}: {exc}")
        finally:
            # 幂等兜底：无论上面走哪条路，房间都必须回到可用状态。
            self.running = False
            self._addressed = set()
            for aid in self.agents:
                if self.status.get(aid) == "发言中":
                    self.status[aid] = "在线"
            await self.push_state()

    # ---------- 连接 ----------

    async def connect_all(self) -> None:
        """并行启动所有 agent。

        原来是串行 await——npx 首次拉包十几秒，四个加起来就一两分钟，全是白等。
        改成 gather 之后总耗时 = 最慢那个（实测 40s+ → 12s 左右）。
        """
        for aid, agent in self.agents.items():
            self.status[aid] = "连接中…"
        await self.push_state()

        await asyncio.gather(*(self.start_agent(aid) for aid in list(self.agents)),
                             return_exceptions=True)

    async def start_agent(self, aid: str) -> None:
        """启动单个 agent 并做一次配置发现。失败只改状态，不抛。

        抽成方法是为了让「热添加」能复用：刚加进来的 agent 只建了对象、
        进程还没起，状态停在「未连接」。以前用户得自己再点一次
        「重连全部 agent」——那会把**所有** agent 的 session 掐掉重建，
        为了加一个人打断另外五个，代价不对。
        """
        agent = self.agents.get(aid)
        if agent is None:
            return
        try:
            await asyncio.wait_for(agent.start(), timeout=240)
            self.status[aid] = "在线"
            # 接回失败必须单独留一条：它意味着这个 agent 现在是「失忆重起」的，
            # 而它接下来照样能正常说话，光看转录完全看不出来。
            # 原因原文要留着 —— "no rollout found" 是那次会话本来就没内容，
            # 别的报错（没登录、进程挂了）则说明线程还在，值得再试。
            if getattr(agent, "resume_error", ""):
                self.audit.write(
                    agent=aid, turn_seq=self.seq, event="session_restore",
                    tool="session", title=(agent.resume_session_id or "")[:36],
                    status="fail", decision="allow",
                    rule=f"上次会话没接回，已新开一个：{agent.resume_error}",
                    initiator=getattr(self, "_initiator", "person"),
                    session=agent.session_id or "",
                )
            self.persist_sessions()
            raw = getattr(agent, "session_info", None) or {}
            await self.opts.discover(agent, raw)
        except Exception as exc:  # noqa: BLE001
            self.status[aid] = f"不可用: {type(exc).__name__}"
            log(f"[{aid}] 连接失败: {exc}")
        await self.push_state()

    async def close(self) -> None:
        self.running = False
        for agent in self.agents.values():
            await agent.stop()
        self.store.close()

    # ---------- 历史 ----------

    def export_markdown(self) -> Path:
        return self.store.export_md(title=f"ACP Room · {self.store.dir.name}")

    async def clear(self) -> None:
        """清空当前对话。旧文件改名留档，不删。"""
        self.store.clear()
        # 记忆是跟着对话走的。对话都留档了，记忆还留着 = 新房间一开口就
        # 引经据典一段没人说过的话。
        self.memory.clear()
        # G6 同理：房间侧作废了，之前推进 agent 长期记忆的那部分也必须收回，
        # 否则表现是"明明清空了，它下次开会话还记得"。只收回我们自己写的那段。
        for o in self.agent_memory.retract():
            if o.get("status") != "written":
                continue
            self.audit.write(
                agent=o["agent"], turn_seq=self.seq, event="agent_memory",
                tool="agent_memory", title="收回", status="ok", decision="allow",
                rule=f"房间清空 → {o['detail']}", initiator="person",
                session=self.session_of(o["agent"]),
            )
        self.transcript.clear()
        self.seq = 0
        for aid in self.agents:
            self.seen[aid] = 0
        # store.clear() 把会话映射一起作废了，内存里这份也必须跟着清掉：
        # persist_sessions 是「存档 + 这次的变化」合并写的，留着旧副本
        # 等于下一次落盘把刚作废的 id 又捞回来，agent 的上下文接回一个
        # 已经没有转录的旧会话。
        self.saved_sessions = {}
        self.spoke.clear()
        await self.push_state()
