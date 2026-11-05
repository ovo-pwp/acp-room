"""room.py 与 core 的接缝测试 —— 不起 ACP 进程，只验证「调用参数对不对」。

会 import room 的测试一共四个（本文件、test_room_pacing、test_audit、
test_agent_memory），全部用 `Room.__new__` 造假房间。本文件是其中最薄的：
  - 不构造完整 Room（那要 4 个 ACP 进程 + 读 config.json）
  - 只验证 room 侧「我到底把什么交给了 core」

如果哪天这些用例开始需要 mock ACP，说明 core 的边界又被破了，
应该回头改 core，而不是在这里加 mock。
"""

import asyncio
import re
from pathlib import Path

import pytest

from core import context as ctx
from core.arbitration import arbitrate


class FakeRoom:
    """只具备 room 里被 core 用到的几个属性。"""

    def __init__(self, turns, agents=("pi", "omp", "claude", "codex")):
        self.transcript = turns
        self.agents = {a: None for a in agents}
        self.order = list(agents)
        self.seen = {}
        self.specs = {}


def test_room侧组装与core一致(turn_factory):
    """room 的 _prompt_for 逻辑等价于直接用 core 组装（含游标推进）。"""
    from conftest import FakeTurn

    turns = [turn_factory(1, "human", "问题"), turn_factory(2, "omp", "回答")]
    plan = ctx.build_body(ctx.pending_since(turns, 0, "pi"), "")
    assert plan.total == 2
    out = ctx.assemble("RULES", "SKILLS", plan, note="", wait_hint="WAIT")
    assert out.startswith("RULES")
    assert "回答" in out
    assert out.endswith("WAIT")


def test_游标语义_推进后为空(turn_factory):
    turns = [turn_factory(1, "human", "a"), turn_factory(2, "omp", "b")]
    seen = 0
    first = ctx.pending_since(turns, seen, "pi")
    seen = len(turns)
    second = ctx.pending_since(turns, seen, "pi")
    assert len(first) == 2
    assert second == []


def test_仲裁对room风格的名册可用(turn_factory):
    roster = [("pi", "Pi"), ("omp", "OMP")]
    g = arbitrate(text="@pi 你来说", explicit=None, roster=roster)
    assert g.speakers == ["pi"]


def test_activity接受FakeTurn(turn_factory):
    turns = [turn_factory(i, "pi" if i < 3 else "omp", "x") for i in range(6)]
    assert ctx.activity(turns, ["pi", "omp"]) == {"pi": 3, "omp": 3}


def test_wait正则与room里的一致():
    """room 和 core 必须用同一个正则，否则 [WAIT] 的识别会两边不一致。"""
    import room

    assert room._WAIT_RE.pattern == re.compile(
        r"^\s*\**\s*\[WAIT\]\s*\**\s*", re.I).pattern

    for text in ["[WAIT] 不说了", "**[WAIT]** 附理由", "[wait] 小写"]:
        assert ctx.wait_reason(text, room._WAIT_RE) is not None

    for text in ["我同意", "前文 [WAIT] 但不是开头"]:
        assert ctx.wait_reason(text, room._WAIT_RE) is None


def _fake_room_for_run(mode):
    """造一个能跑通 Room.run 的壳：四个 agent 立刻返回固定文本。

    只用鸭子类型，不构造真的 Room —— 那要 4 个 ACP 进程。
    """
    import room as room_mod

    class FakeAgent:
        busy = False
        session_id = "S-fake"      # persist_sessions 会读它

        async def prompt(self, prompt, timeout=None, images=None):
            return {"stopReason": "end_turn", "usage": {"totalTokens": 1}}

        async def cancel(self):
            return None

    class FakeAudit:
        async def write(self, **kw):
            return None

    class FakeSkills:
        enabled = False

        def wants(self, text, agent_id=""):
            return []

        def inject(self, *a, **k):
            return None

    class FakeStore:
        def append(self, turn):
            return None

    class FakeMemory:
        """G1：_prompt_for 会读记忆。空记忆必须注入空串，不占 prompt。"""

        def block(self):
            return ""

        def add(self, *a, **k):
            return {}

        def rows(self):
            return []

    class FakeAttachments:
        def to_acp_blocks(self, metas):
            return []

    r = room_mod.Room.__new__(room_mod.Room)
    ids = ["pi", "omp", "claude", "codex"]
    r.agents = {a: FakeAgent() for a in ids}
    r.order = list(ids)
    r.specs = {a: room_mod.AgentConfig(id=a, name=a.title(), cmd=["x"], cwd=".")
               for a in ids}
    r.transcript = []
    r.seen = {a: 0 for a in ids}
    r.spoke = set()          # persist_sessions 用它把关，Room.__new__ 造的假房间也得有
    r.status = {a: "在线" for a in ids}
    r.streaming = {a: "" for a in ids}
    r.sockets = set()
    r.running = False
    r.seq = 0
    r.parallel = True
    r.turn_timeout = 5
    r.audit = FakeAudit()
    r.skills = FakeSkills()
    r.store = FakeStore()
    r.memory = FakeMemory()
    r.attachments = FakeAttachments()

    # `_commit` 会问当前模型、`_execute` 结束时会把 sessionId 落盘。
    # 这两件事都不是本文件要验的（会话持久化有自己的用例，见下面 session 组），
    # 这里给最小桩，免得假房间因为缺属性而把真断言淹掉。
    class FakeOpts:
        models = {"pi": "glm-5"}

        def model_of(self, agent_id):
            return self.models.get(agent_id, "")

    r.opts = FakeOpts()
    r.saved_sessions = {}
    r.persist_sessions = lambda: None
    r.conv_mode = mode
    r.conv_max_per_agent = 3
    r._addressed = set()

    # P1–P4 新加的属性。`Room.__new__` 的壳子缺一个就会在**别的**用例里炸
    # （G5 加 self.spoke 时踩过同一个坑），所以一次补齐：
    #   turn_caps / turn_t0 / first_chunk → _execute 的耗时记账
    #   run_budget / run_started          → run() 入口的墙钟预算（0 = 不设限）
    r.turn_caps = {}
    r.turn_t0 = {}
    r.first_chunk = {}
    r.run_budget = 0
    r.run_started = 0.0

    async def push_state():
        return None

    r.push_state = push_state

    async def one_turn(aid, note="", initiator="person"):
        r.streaming[aid] = "我说一句。"
        return room_mod.Turn(seq=0, speaker=aid, name=aid, text="我说一句。",
                             ts=0.0, tokens=1, ms=1)

    r._one_turn = one_turn
    return r


@pytest.mark.parametrize("mode", ["free", "round"])
def test_run结束后running必须归位(mode):
    """回归：自由发言分支曾用 `return` 绕过 finally，running 永久卡在 True。

    实测症状是 /api/state 一直报 running=true，之后每次 /api/say 都 500
    「已经在跑了一轮」，而日志里那一场早就结束了 —— 房间只能重启才能再用。
    """
    r = _fake_room_for_run(mode)
    asyncio.run(r.run(rounds=1))
    assert r.running is False


def test_run抛异常时running也要归位():
    """被点名强制追问、ACP 断线等都可能抛出去；抛了也不能把房间锁死。"""
    r = _fake_room_for_run("free")

    async def boom(aid, note="", initiator="person"):
        raise RuntimeError("模拟 agent 崩了")

    r._one_turn = boom
    with pytest.raises(RuntimeError):
        asyncio.run(r.run(rounds=1))
    assert r.running is False


@pytest.mark.parametrize("status", ["wait", "empty", "blocked"])
def test_没说话就不占转录(status):
    """wait / empty / blocked 都不该占 transcript 序号，也不该广播成一条发言。

    以前空正文会被填一句占位文本当成 ok 提交，转录里和真回答长得一样，
    翻记录看不出「它其实一个字没说」——实测 pi 因网关 401 连续两轮如此。
    blocked 是同一类问题的另一面：吐出来的不是话，是网关错误。
    """
    r = _fake_room_for_run("round")
    r._addressed = set()
    turn = room_seam_turn(status)
    asyncio.run(r._commit(turn))
    assert r.transcript == []
    assert r.seq == 0


def test_网关错误不会被写进转录():
    """回归：omp 的 402 / codex 的 ACP 错误曾经都是 ok 轮次。

    错误文本一旦进了转录，就变成会被别人反复引用的"发言"——实测 claude 和 pi
    真的围绕 omp 那条 401 讨论了好几轮。
    """
    r = _fake_room_for_run("round")

    async def bad_reply(aid, note="", initiator="person"):
        r.streaming[aid] = ""
        return room_seam_turn("blocked", speaker=aid, text=(
            "402 Insufficient Balance (request_id: ff0b28a6-3975-448d-8793-5aa0d32bbf63) "
            "(type=insufficient_balance_error param=invalid_request_error)"))

    r._one_turn = bad_reply
    asyncio.run(r.run(rounds=1))
    assert r.transcript == []
    assert r.running is False


def test_空正文不占用上下文也不假装说过():
    """empty 归零之后，自由发言的 spoke 计数不该把它算进去。"""
    r = _fake_room_for_run("free")

    async def empty_turn(aid, note="", initiator="person"):
        r.streaming[aid] = ""
        return room_seam_turn("empty", speaker=aid)

    r._one_turn = empty_turn
    asyncio.run(r.run(rounds=1))
    assert r.running is False
    assert r.transcript == []


def room_seam_turn(status, speaker="pi", text=""):
    import room as room_mod

    return room_mod.Turn(seq=0, speaker=speaker, name=speaker.title(), text=text,
                         ts=0.0, status=status)


def test_execute把网关错误标成blocked并广播notice():
    """守卫在 _execute 里的行为：标 blocked、正文置空、广播一条 notice。

    正文必须清空——否则"错误"仍然会通过 Turn 对象流出去（比如被 store 落盘）。
    notice 是给用户看的唯一线索：被拦下的轮次不进转录，界面上看不到任何东西，
    没有这条提示就只剩"某个 agent 莫名沉默"。
    """
    r = _fake_room_for_run("round")
    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)

    r.broadcast = fake_broadcast

    async def go():
        # _execute 的正文来自 streaming（由 ACP 回调写入），不是 prompt 的返回值
        r.streaming["pi"] = (
            "402 Insufficient Balance (request_id: ff0b28a6-3975-448d-8793-5aa0d32bbf63) "
            "(type=insufficient_balance_error param=invalid_request_error)")
        return await r._execute("pi", "hi")

    turn = asyncio.run(go())
    assert turn.status == "blocked"
    assert turn.text == "", "被拦下的正文必须清空，不能顺着 Turn 流出去"
    notices = [m for m in sent if m.get("type") == "notice"]
    assert len(notices) == 1
    assert notices[0]["level"] == "warn"
    assert "gateway_error" in notices[0]["text"]
    assert r.status["pi"] == "在线", "拦下之后 agent 状态要回到在线，不能卡在发言中"


def test_execute不拦正常回答():
    r = _fake_room_for_run("round")

    async def go():
        r.streaming["pi"] = "我同意这个判断，但有个前提要先说清楚。"
        return await r._execute("pi", "hi")

    turn = asyncio.run(go())
    assert turn.status == "ok"
    assert turn.text.startswith("我同意")


def test_看门狗能捞回僵住的轮次():
    """run() 里的 finally 只在它**返回**时才执行。

    如果某个 await 卡死（ACP 不回包、agent 僵住），run() 永不返回，
    finally 永不执行，房间照样只能重启——这正是那 16 分钟卡死的形状。
    看门狗必须在 run() 之外兜住它。
    """
    r = _fake_room_for_run("round")
    r.run_watchdog = 0.3

    async def hang(*a, **k):
        await asyncio.sleep(60)

    r.run = hang
    asyncio.run(r._guarded_run(None, 1, ""))
    assert r.running is False


def test_看门狗没到时正常跑完不误伤():
    r = _fake_room_for_run("round")
    r.run_watchdog = 30
    asyncio.run(r._guarded_run(None, 1, ""))
    assert r.running is False


# ---------- 自由发言②：串行 + 游标 → 后说话的人看得到本轮前面的话 ----------


def test_后说话的人能看到本轮前面说过的内容():
    """这是"接着某人的话反驳"能发生的**前提**。

    自由模式是串行的：`_commit` 先把这一条写进 transcript，下一个人才组装
    prompt。所以后说话的人，游标扫到的内容里天然包含本轮前面那几句。

    如果这里断了（比如为了"并行提速"把一轮改成 gather），四个 agent 就只会
    对着人类的原话各说一段，谁也接不上谁——表面上还是四个人在说话，
    实际上已经不是对话了。实测过一次反驳链（pi 反对 claude 的后半句），
    靠的就是这条性质。
    """
    import room as room_mod

    r = _fake_room_for_run("free")
    # claude 先说了这一句，并且已经提交进 transcript
    r.transcript.append(room_mod.Turn(
        seq=1, speaker="claude", name="Claude",
        text="Pi 先说——他拆题要做的判断最少", ts=0.0, tokens=1, ms=1))
    r.seq = 1

    # 轮到 pi：它必须看得到 claude 这句
    prompt = r._prompt_for("pi", "")
    assert "拆题要做的判断最少" in prompt
    assert prompt.count("claude") + prompt.count("Claude") >= 1
    # 游标推进到 transcript 末尾
    assert r.seen["pi"] == 1


def test_看过的不再重复塞进prompt():
    """游标推进之后，同一句不该在下一次 prompt 里再出现一遍。

    不这样的话，agent 每次发言都会被塞进越来越长的历史，
    token 会随步数平方增长。
    """
    import room as room_mod

    r = _fake_room_for_run("free")
    r.transcript.append(room_mod.Turn(
        seq=1, speaker="claude", name="Claude",
        text="独一无二的标记句_ALPHA", ts=0.0, tokens=1, ms=1))
    r.seq = 1

    first = r._prompt_for("pi", "")
    assert "独一无二的标记句_ALPHA" in first

    second = r._prompt_for("pi", "")
    assert "独一无二的标记句_ALPHA" not in second


def test_自己说过的话不会被喂回给自己():
    """agent 自己的上下文在 ACP session 里，不需要每条都重发。"""
    import room as room_mod

    r = _fake_room_for_run("free")
    r.transcript.append(room_mod.Turn(
        seq=1, speaker="pi", name="Pi",
        text="我自己刚才说的_SELF", ts=0.0, tokens=1, ms=1))
    r.seq = 1

    prompt = r._prompt_for("pi", "")
    assert "我自己刚才说的_SELF" not in prompt


def test_自由发言默认串行而不是并行():
    """自由模式必须串行：并行就没有"下一个发言人"这回事了。

    `parallel_turns` 只对 round 模式生效——这是一处容易被误改的地方，
    因为看名字像是"全局开关"。
    """
    src = open("room.py", encoding="utf-8").read()
    start = src.index('if self.conv_mode == "free":')
    # 自由分支的边界 = 它之后第一个 round 模式的循环。
    # 不能用 `index("_parallel_round")` 来截断：真有人把它搬进自由分支时，
    # 那样截出来的片段恰好**不含**它，断言会假通过。
    end = src.index("for r in range(rounds):", start)
    free_block = src[start:end]
    assert "parallel" not in free_block, (
        "自由发言分支里出现了并行相关代码——并行会破坏"
        "「后说话的人能看到前面说过的话」这条性质，"
        "而那条性质正是链式反驳能发生的前提")
    assert "_free_session" in free_block, "截取范围不对，没抓到自由分支本体"



def test_sessionId变了必须留一条审计():
    """换模型、重启、进程崩过都会换 sessionId，而 agent 正文照样顺。

    没有这条审计，「它其实失忆了」在记录里完全看不出来。
    """
    import room as room_mod

    class A:
        def __init__(self, sid):
            self.session_id = sid

    class Audit:
        def __init__(self):
            self.rows = []

        def write(self, **kw):
            self.rows.append(kw)
            return None

    class Store:
        def __init__(self):
            self.saved = None

        def save_sessions(self, mapping):
            self.saved = dict(mapping)

    r = room_mod.Room.__new__(room_mod.Room)
    r.agents = {"pi": A("S-1")}
    r.saved_sessions = {"pi": "S-0"}
    r.spoke = {"pi"}          # 这次真的开过口，新 id 才有资格顶掉旧的
    r.audit = Audit()
    r.store = Store()
    r.seq = 7
    r._initiator = "person"
    room_mod.Room.persist_sessions(r)

    assert [x["event"] for x in r.audit.rows] == ["session_switch"]
    assert r.audit.rows[0]["title"] == "S-0 → S-1"
    assert r.audit.rows[0]["agent"] == "pi"
    assert r.audit.rows[0]["session"] == "S-1"
    assert r.store.saved == {"pi": "S-1"}
    # 落过盘后不该第二次再报同一次变化
    room_mod.Room.persist_sessions(r)
    assert len(r.audit.rows) == 1


def test_首次建会话不算变化():
    """否则每次启动都刷五条「session_switch」噪音，真正的变化就淹没了。"""
    import room as room_mod

    class A:
        session_id = "S-1"

    class Audit:
        def __init__(self):
            self.rows = []

        def write(self, **kw):
            self.rows.append(kw)

    class Store:
        def save_sessions(self, mapping):
            return None

    r = room_mod.Room.__new__(room_mod.Room)
    r.agents = {"pi": A()}
    r.saved_sessions = {}
    r.spoke = {"pi"}
    r.audit = Audit()
    r.store = Store()
    r.seq = 0
    room_mod.Room.persist_sessions(r)
    assert r.audit.rows == []


def test_没开过口的会话不能顶掉存着的旧id():
    """codex 的 rollout 要等第一轮跑完才落盘，所以「接回失败 → 新开一个」是常态。

    这种空壳会话没有任何可恢复的东西，要是让它覆盖掉磁盘上真有序列的旧 id，
    那次会话就永久接不回来了（实测审计里 01a1a5c9 就是这么丢的）。
    """
    import room as room_mod

    class A:
        session_id = "S-EMPTY-NEW"

    class Audit:
        def __init__(self):
            self.rows = []

        def write(self, **kw):
            self.rows.append(kw)

    class Store:
        def __init__(self):
            self.saved = None

        def save_sessions(self, mapping):
            self.saved = dict(mapping)

    r = room_mod.Room.__new__(room_mod.Room)
    r.agents = {"codex": A()}
    r.saved_sessions = {"codex": "S-OLD-有rollout"}
    r.spoke = set()            # 这个进程里 codex 一次都没说话
    r.audit = Audit()
    r.store = Store()
    r.seq = 0
    room_mod.Room.persist_sessions(r)

    assert r.store.saved is None       # 一个字都没写
    assert r.audit.rows == []          # 也不该刷 session_switch 噪音
    assert r.saved_sessions == {"codex": "S-OLD-有rollout"}

    # 一旦真开过口，新 id 就该留下（旧线程这次确实被弃用了）
    r.spoke = {"codex"}
    room_mod.Room.persist_sessions(r)
    assert r.store.saved == {"codex": "S-EMPTY-NEW"}
    assert [x["event"] for x in r.audit.rows] == ["session_switch"]


def test_每条发言带上当时的sessionId(tmp_path):
    """turns.jsonl 里的 session 列是「换过模型」唯一的可查证据。"""
    import room as room_mod
    from core.store import Store

    r = _fake_room_for_run("free")
    r.store = Store(tmp_path, "seam")
    r.agents["pi"].session_id = "S-9"
    turn = room_mod.Turn(seq=0, speaker="pi", name="Pi", text="说完了", ts=0.0)
    asyncio.run(r._commit(turn))

    assert r.transcript[0].session == "S-9"
    assert r.store.load()[0]["session"] == "S-9"
    r.store.close()


def test_每条发言带上当时的模型(tmp_path):
    """G4：同一个人一小时内可能换三次模型，正文里完全看不出差别。

    复盘「这句为什么这么蠢」时，唯一线索就是这句出自哪个模型，
    所以盖戳必须在 `_commit` 这一个落盘点上做——别处盖会漏，也会盖错。
    """
    import room as room_mod
    from core.store import Store

    r = _fake_room_for_run("free")
    r.store = Store(tmp_path, "seam")
    asyncio.run(r._commit(room_mod.Turn(seq=0, speaker="pi", name="Pi",
                                       text="说完了", ts=0.0)))
    # 人说的话没有模型可标；没发现过配置的 agent 同样是空串
    asyncio.run(r._commit(room_mod.Turn(seq=0, speaker="human", name="我",
                                        text="我说", ts=0.0)))

    assert r.transcript[0].model == "glm-5"
    assert r.transcript[1].model == ""
    rows = r.store.load()
    assert rows[0]["model"] == "glm-5"
    assert rows[1]["model"] == ""
    # 导出稿要带着它，否则导出的 markdown 反而比 jsonl 少信息
    md_text = r.store.export_md().read_text(encoding="utf-8")
    assert "model=glm-5" in md_text
    r.store.close()


def test_重启后模型戳要从磁盘读回来(tmp_path):
    """`_restore` 逐个字段构造 Turn。漏一个字段，重启后那一栏就全空。"""
    import room as room_mod
    from core.store import Store

    r = _fake_room_for_run("free")
    r.store = Store(tmp_path, "seam")
    r.agents["pi"].session_id = "S-9"
    asyncio.run(r._commit(room_mod.Turn(seq=0, speaker="pi", name="Pi",
                                       text="说完了", ts=0.0)))
    r.store.close()

    r2 = _fake_room_for_run("free")
    r2.store = Store(tmp_path, "seam")
    r2.transcript = []
    r2.seq = 0
    room_mod.Room._restore(r2)

    assert r2.transcript[0].model == "glm-5"
    assert r2.transcript[0].session == "S-9"
    r2.store.close()


def test_讨论引擎调的公开入口必须透传initiator():
    """薄封装漏掉 initiator 时，整场讨论在第一个阶段抛 TypeError。

    而且它发生在 asyncio.create_task(go()) 里，没人 await 那个任务，
    界面上的症状只是「点了讨论，什么都没发生」。
    """
    import room as room_mod

    class R:
        def __init__(self):
            self.got = None

        async def _one_turn(self, aid, note, initiator="person"):
            self.got = initiator
            return None

        async def _parallel_round(self, ids, note, initiator="person"):
            self.got = initiator
            return []

    r = R()
    asyncio.run(room_mod.Room.one_turn(r, "pi", "说一句", initiator="discussion"))
    assert r.got == "discussion"
    r.got = None
    asyncio.run(room_mod.Room.parallel_round(r, ["pi"], "说一句", initiator="discussion"))
    assert r.got == "discussion"

    # 讨论引擎那边的调用形状也必须带上这个参数
    src = (Path(__file__).resolve().parent.parent / "core" / "discussion.py").read_text(
        encoding="utf-8")
    assert 'initiator="discussion"' in src


def test_讨论汇总必须写回房间记忆():
    """G1：汇总那段「共识/分歧/下一步」是全场密度最高的产物，不能只活一轮。"""
    from core.discussion import Discussion

    class R:
        running = True
        specs = {}

        def __init__(self):
            self.remembered = []
            self.note = ""

        async def announce(self, text):
            return None

        async def one_turn(self, aid, note, initiator="person"):
            self.note = note
            return type("T", (), {"text": "共识：用 JSONL", "speaker": aid})()

        def remember(self, topic, text, by=""):
            self.remembered.append((topic, text, by))

    r = R()
    asyncio.run(Discussion(r)._synthesis_end(["pi", "hermes"], "要不要上 Docker",
                                             "standard", None))
    assert r.remembered == [("要不要上 Docker", "共识：用 JSONL", "hermes")]
    assert "汇总" in r.note


def test_讨论引擎引用的阶段模板必须都在():
    """这些常量曾经整批丢失（拆到 convergence.py 时漏了 import + 漏了定义）。

    症状是讨论一点就 500，但报错发生在 asyncio 任务里，界面上只是"没反应"。
    名字写死在这里是有意的：这七个是引擎的全部外部文本，缺一个都跑不完一场。
    """
    from core import discussion as dsh

    for name in ("SYNTHESIS", "OXFORD_SYNTHESIS", "MARK_HINT", "DISCUSS_ROUND",
                 "ADVOCATE_JUDGE", "ADVOCATE_ATTACK", "ADVOCATE_DEFEND"):
        text = getattr(dsh, name, None)
        assert isinstance(text, str) and text.strip(), f"core/discussion.py 里 {name} 不存在或为空"


def test_交锋轮模板的占位符必须能填():
    """`DISCUSS_ROUND.format(round=.., total=..)` 是调用方写死的形状。

    模板里改了占位符名字不会立刻炸，只会在讨论跑到第 3 阶段时 KeyError。
    """
    from core.discussion import ADVOCATE_ATTACK, ADVOCATE_JUDGE, DISCUSS_ROUND

    assert "1/3" in DISCUSS_ROUND.format(round=1, total=3)
    assert "多数" in ADVOCATE_JUDGE.format(names="Pi、OMP")
    assert "A 还是 B" in ADVOCATE_ATTACK.format(majority="A 还是 B")


def test_讨论预算能被赋值():
    """`server.py` 每次 /api/discuss 都写 `ROOM.disc.budget = Budget(...)`。

    budget 是转发到 conv 的 property，只有 getter 时赋值抛
    AttributeError —— 整个讨论接口 500，而且和阶段模板丢字是同一类事故。
    """
    from core.discussion import Budget, Discussion

    d = Discussion(room=None)
    d.budget = Budget(max_rounds=2, max_tokens=1234)
    assert d.budget.max_rounds == 2
    assert d.conv.budget.max_tokens == 1234


@pytest.mark.parametrize("method", ["standard", "advocate", "oxford"])
def test_三种讨论法都能跑完整整一场(method):
    """整条编排链的冒烟测试。

    这一场会经过 `_cum_tokens`、七个阶段模板、`budget` setter、
    `one_turn(initiator=)` 这些接缝。2026-11-04 实测：这条链上四处已经断了
    （拆 convergence.py 时漏搬），而讨论是 asyncio 任务里跑的，
    界面上看不出任何异常，只是"点了没反应"。所以这里整场跑一遍，不打桩。
    """
    from core.discussion import Budget, Discussion

    r = _fake_room_for_run("round")
    d = Discussion(r, method=method,
                   budget=Budget(max_rounds=1, max_tokens=100_000, max_seconds=600))
    verdict = asyncio.run(d.run("要不要上 Docker", ["pi", "omp", "claude", "codex"]))
    assert verdict.stop is True
    assert verdict.kind in ("completed", "consensus", "deadlock"), verdict.kind
    assert r.running is False, "跑完必须把房间卫兵放回 False"
