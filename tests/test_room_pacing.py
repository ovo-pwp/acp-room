"""P1–P4 在 room.py 里的接缝：耗时记账、墙钟预算、同批并行、按家的单轮上限。

复用 `test_room_seams._fake_room_for_run` 那个假房间（四个 agent、不起 ACP 进程），
只补这四件事新需要的属性。测的是 room 自己的控制流——
「什么时候不再等」「到点了记什么」「两个人什么时候并行、什么时候串行」，
ACP 协议本身在 tests/test_session_resume.py 用真子进程测。

值得单独一个文件的原因：这四处出错都不报错。症状是
「一句话串了四分钟」「超时了但审计里 ms=0」「该并行的还是排着」。
"""

import asyncio
import time

import pytest
from test_room_seams import _fake_room_for_run

import room as room_mod
from room import Turn


class RecordingAudit:
    def __init__(self):
        self.rows = []

    async def write(self, **kw):
        self.rows.append(kw)

    def events(self, event):
        return [r for r in self.rows if r.get("event") == event]


def pacing_room(mode="free", agents=("pi", "omp", "claude", "codex")):
    """假房间 + P1–P4 需要的那几样属性（都是 __init__ 里新加的，壳子得自己补）。"""
    r = _fake_room_for_run(mode)
    r.order = list(agents)
    r.agents = {a: _idle_agent() for a in agents}
    r.specs = {a: room_mod.AgentConfig(id=a, name=a.title(), cmd=["x"], cwd=".")
               for a in agents}
    r.turn_caps = {}
    r.turn_t0 = {}
    r.first_chunk = {}
    r.run_budget = 0
    r.run_started = 0.0
    # 直接调 `_free_session` / `_execute` 的用例绕过了 run() 的入口，
    # 而循环第一行就是 `if not self.running: break`——不点亮它，测的是空转。
    r.running = True
    r.audit = RecordingAudit()
    r.notices = []

    async def announce(text, *a, **kw):
        r.notices.append(text)

    r.announce = announce
    return r


def _idle_agent():
    class A:
        busy = False
        session_id = "S-fake"
        cancelled = False

        async def prompt(self, prompt, timeout=None, images=None):
            return {"stopReason": "end_turn", "usage": {"totalTokens": 1}}

        async def cancel(self):
            self.cancelled = True
    return A()


# ---------------------------------------------------------------- P4 单轮上限

def test_单轮上限按家取_没配就用全局():
    r = pacing_room()
    r.turn_timeout = 120
    r.turn_caps = {"codex": 60}
    assert r.turn_timeout_for("codex") == 60
    assert r.turn_timeout_for("pi") == 120


def test_单轮上限为0或空时退回全局():
    """config 里写 `turn_timeout: 0`（= 不限）时不该把 0 传给 wait_for，
    否则每一轮都立刻超时。"""
    r = pacing_room()
    r.turn_timeout = 120
    r.turn_caps = {"codex": 0, "pi": None}
    assert r.turn_timeout_for("codex") == 120
    assert r.turn_timeout_for("pi") == 120


# ---------------------------------------------------------------- P1 耗时记账

def test_超时这一轮记下等了多久():
    """回归：超时分支以前不传 ms，「等了 120s 没等到」在数据里和「秒回」同形。"""
    class Hang:
        busy = False
        session_id = "S-hang"
        cancelled = False
        cap_seen = None

        async def prompt(self, prompt, timeout=None, images=None):
            self.cap_seen = timeout
            raise asyncio.TimeoutError()

        async def cancel(self):
            self.cancelled = True

    r = pacing_room()
    agent = Hang()
    r.agents["pi"] = agent
    r.turn_caps = {"pi": 7}

    turn = asyncio.run(r._execute("pi", "随便说点什么"))
    assert turn.status == "cancelled"
    assert agent.cap_seen == 7                    # 按家的上限传下去了
    assert agent.cancelled is True
    assert r.status["pi"] == "超时"
    assert turn.ttft_ms == 0                      # 一个字都没开始
    assert 0 <= turn.ms < 5000

    rows = r.audit.events("turn_timeout")
    assert len(rows) == 1
    assert rows[0]["tool"] == "prompt" and rows[0]["title"] == "7s"
    assert "一个字都没开始" in rows[0]["rule"]
    assert rows[0]["ms"] == turn.ms and rows[0]["ttft_ms"] == 0
    # turn_t0 必须清掉：残留会让下一轮"看起来一秒就开口"
    assert "pi" not in r.turn_t0


def test_超时但已经开了口的说法不一样():
    """ttft=0 和 ttft>0 是两种故障（网关没响应 / 说了半句断流），
    这个区别只有超时分支当场能记，事后从界面查不回来。"""
    class HalfThenHang:
        busy = False
        session_id = "S-half"

        async def prompt(self, prompt, timeout=None, images=None):
            r.first_chunk["pi"] = time.time() + 0.01
            raise asyncio.TimeoutError()

        async def cancel(self):
            return None

    r = pacing_room()
    r.agents["pi"] = HalfThenHang()
    turn = asyncio.run(r._execute("pi", "说一半"))
    assert turn.status == "cancelled"
    assert r.audit.events("turn_timeout")[0]["rule"].count("说了半句就停") == 1


def test_正常发言也带耗时():
    """这条钉的是那个原始 bug 本身：Turn 有 ms，但审计写入点没传，
    结果审计里所有行 ms 都是 0，"谁慢"无从算起。"""
    class Talk:
        busy = False
        session_id = "S-ok"

        async def prompt(self, prompt, timeout=None, images=None):
            r.first_chunk["pi"] = time.time() + 0.02
            await asyncio.sleep(0.03)               # 真的花掉 30ms，ms 才不会是 0
            r.streaming["pi"] = "我的判断是先量一遍再说。"
            return {"stopReason": "end_turn", "usage": {"totalTokens": 12}}

    r = pacing_room()
    r.agents["pi"] = Talk()
    turn = asyncio.run(r._execute("pi", "你怎么看"))
    assert turn.status == "ok" and turn.tokens == 12
    assert turn.ms >= 20
    assert 0 < turn.ttft_ms < 5000


def test_首字时刻只记第一次到达():
    """第二个、第三十个 chunk 不该把 first_chunk 往后推——那 TTFT 就成了总耗时。"""
    r = pacing_room()
    now = time.time()
    r.turn_t0["pi"] = now
    r.first_chunk.setdefault("pi", now + 0.5)
    r.first_chunk.setdefault("pi", now + 9.0)     # 模拟后续 chunk 也调这一行
    assert r.turn_ttft("pi") == pytest.approx(500, abs=60)


def test_本轮耗时从turn_t0算():
    r = pacing_room()
    r.turn_t0["pi"] = time.time() - 3
    assert 2900 < r.turn_elapsed("pi") < 4000
    assert r.turn_elapsed("omp") == 0             # 没开过这一轮 = 0，不是负数


# ---------------------------------------------------------------- P2 墙钟预算

def test_到点就收_并点名还没轮到的人():
    r = pacing_room()
    r.run_budget = 1
    r.run_started = time.time() - 5

    async def never(aid, note="", initiator="person"):
        raise AssertionError("过点了不该再跑一步")

    r._one_turn = never
    r._parallel_round = never
    asyncio.run(r._free_session(note="开报个数", initiator="person",
                                rounds=1, opening_ids=["pi"]))

    rows = r.audit.events("run_budget")
    assert len(rows) == 1 and rows[0]["tool"] == "budget"
    assert "还没轮到" in rows[0]["rule"]
    assert r.notices and "到点了" in r.notices[0]
    # 播报必须点名叫人：「到点了」只说明发生了什么，
    # 「Pi、Omp 还没轮到」才说明接下来该干什么。
    assert "Pi" in r.notices[0] and "Omp" in r.notices[0]


def test_没设预算时不拦正常跑():
    r = pacing_room()

    async def one(aid, note="", initiator="person"):
        return Turn(seq=0, speaker=aid, name=aid.title(),
                    text=f"{aid} 说完了", ts=time.time(), status="ok", ms=10)

    r._one_turn = one
    asyncio.run(r._free_session(note="说一句", initiator="person",
                                rounds=1, opening_ids=["pi"]))
    assert r.audit.events("run_budget") == []
    assert r.audit.events("turn_pick")


def test_两种模式都把预算起点落在run入口():
    for mode in ("round", "free"):
        r = pacing_room(mode=mode)
        assert r.run_started == 0.0
        asyncio.run(r.run(rounds=1))
        assert r.run_started > 0, f"{mode} 模式没记 run_started"
        assert r.running is False


def test_轮询模式到点也停():
    """界面写的是"轮数"，但 4 人 × 3 轮串行一样能跑到四分钟。
    只在自由发言里设预算 = 漏掉默认那条路径。

    `run()` 在入口把 `run_started` 归零，所以这里模拟"第一轮就把预算吃光"：
    `_one_turn` 里把时钟往回拨，第二轮就该被拦下。
    """
    r = pacing_room(mode="round", agents=("pi", "omp"))
    r.run_budget = 30
    r.parallel = False
    calls = []

    async def one(aid, note="", initiator="person"):
        calls.append(aid)
        if len(calls) == 1:
            r.run_started = time.time() - 31     # 第一轮就把预算跑完了
        return Turn(seq=0, speaker=aid, name=aid.title(), text=f"{aid} 说一句",
                    ts=time.time(), status="ok", ms=10)

    r._one_turn = one
    asyncio.run(r.run(speakers=["pi", "omp"], rounds=3, note="开始"))
    # 预算按**轮**检查，不在一轮中间打断别人：3 轮 × 2 人只跑了第 1 轮。
    assert calls == ["pi", "omp"]
    assert len(r.audit.events("run_budget")) == 1
    assert r.notices and "到点了" in r.notices[0]
    assert r.running is False


# ---------------------------------------------------------------- P3 同批并行

def test_一句点到两个人时并行跑一次():
    calls = {"one": [], "batch": [], "addressed": None}

    async def one(aid, note="", initiator="person"):
        calls["one"].append((aid, note))
        text = ("@omp @claude 你们俩怎么看" if aid == "pi"
                else f"{aid} 说完了")
        return Turn(seq=0, speaker=aid, name=aid.title(), text=text,
                    ts=time.time(), status="ok", ms=10)

    async def batch(ids, note="", initiator="person"):
        calls["batch"].append(list(ids))
        calls["addressed"] = set(r._addressed)
        return [await one(aid, note, initiator=initiator) for aid in ids]

    r = pacing_room()
    r._one_turn = one
    r._parallel_round = batch
    asyncio.run(r._free_session(note="开报个数", initiator="person",
                                rounds=1, opening_ids=["pi"]))

    assert calls["one"][0] == ("pi", "开报个数")     # note 只给第 0 步
    assert calls["batch"] == [["omp", "claude"]]     # 没被拆成两次单人
    assert calls["addressed"] == {"omp", "claude"}   # 整批都算被点名，不许 wait
    picks = [row for row in r.audit.events("turn_pick") if row["agent"] == "omp"]
    assert picks and "batch" in picks[0]["rule"]
    assert r.audit.events("turn_batch"), "成批要单独留一行，事后才查得到谁和谁并行"


def test_一个人接话时不并成批():
    seen = []

    async def one(aid, note="", initiator="person"):
        seen.append(aid)
        text = "@omp 你怎么看" if aid == "pi" else "结束"
        return Turn(seq=0, speaker=aid, name=aid.title(), text=text,
                    ts=time.time(), status="ok", ms=10)

    async def batch(ids, note="", initiator="person"):
        raise AssertionError("只点到一个人不该走并行")

    r = pacing_room()
    r._one_turn = one
    r._parallel_round = batch
    asyncio.run(r._free_session(note="开始", initiator="person",
                                rounds=1, opening_ids=["pi"]))
    assert seen[:2] == ["pi", "omp"]


def test_批次里的人按名册顺序落盘():
    """**并行跑、按名册顺序提交。**

    claude 0.01s 就回来、pi 0.12s 才回来，转录仍然必须是 pi→omp→claude。
    要是这里改成"谁先回来谁先写"，下一个人接的就是随机那个人说的话，
    同一句提问两次跑出来的对话会分叉——而且看不出来。
    """
    class Delayed:
        def __init__(self, aid, delay, room):
            self.aid, self.delay, self.room = aid, delay, room
            self.busy = False
            self.session_id = f"S-{aid}"

        async def prompt(self, prompt, timeout=None, images=None):
            await asyncio.sleep(self.delay)
            self.room.streaming[self.aid] = f"{self.aid} 的看法是：先量一遍再说。"
            return {"stopReason": "end_turn", "usage": {"totalTokens": 3}}

        async def cancel(self):
            return None

    r = pacing_room(agents=("pi", "omp", "claude"))
    for aid, delay in (("pi", 0.12), ("omp", 0.06), ("claude", 0.01)):
        r.agents[aid] = Delayed(aid, delay, r)

    t0 = time.time()
    turns = asyncio.run(r._parallel_round(["pi", "omp", "claude"], "开报个数"))
    spent = time.time() - t0

    assert [t.speaker for t in r.transcript] == ["pi", "omp", "claude"]
    assert [t.speaker for t in turns] == ["pi", "omp", "claude"]
    # 三句都在：串行要 0.19s，并行只花最慢那一条的时间
    assert len(r.transcript) == 3
    assert spent < 0.17
    assert all(t.ms > 0 for t in turns)

