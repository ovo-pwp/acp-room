"""G2：重启后接回上次的 ACP 会话。

测的是 core/agent.py 的 open_session() 四条分支 + core/store.py 的会话映射落盘。
用 tests/stub_acp_agent.py（一个能开关能力的假 ACP server）而不是真 agent：
resume/load 的差异只有真对话才测得出来，而真 agent 慢、花钱、且未必支持。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from core.agent import AcpAgent, AgentConfig
from core.store import Store

STUB = Path(__file__).resolve().parent / "stub_acp_agent.py"
ROOT = STUB.parents[1]


def _agent(caps: str):
    received: list[dict] = []

    async def on_update(evt):
        received.append(evt)

    async def on_permission(evt):
        return {"outcome": {"outcome": "cancelled"}}

    cfg = AgentConfig(
        id="stub", name="Stub", cmd=[sys.executable, str(STUB)],
        cwd=str(ROOT), env={"STUB_CAPS": caps, "PYTHONUTF8": "1"},
    )
    return AcpAgent(cfg, on_update, on_permission), received


async def _open(caps: str, resume_sid: str | None):
    """起进程、按 resume_sid 走一次 open_session，无论成败都把进程收掉。"""
    agent, received = _agent(caps)
    agent.resume_session_id = resume_sid
    try:
        await agent.start()
        return agent, received
    except Exception:
        await agent.stop()
        raise


async def _close(agent) -> None:
    await agent.stop()


# ---------- open_session 的分支 ----------

def test_支持resume时优先resume且不回放():
    async def run():
        agent, received = await _open("resume", "S-OLD")
        try:
            assert agent.session_origin == "resume"
            assert agent.session_id == "S-OLD"
            # 真适配器（codex/claude）resume 成功时**不回 sessionId**，
            # 所以 id 是从请求里留下的；这种情况绝不能记成"没接回"。
            assert agent.resume_error == ""
            # resume 不该有任何 update 落到界面
            assert received == []
        finally:
            await _close(agent)
    asyncio.run(run())


def test_只支持load时吞掉回放但不吞以后的():
    async def run():
        agent, received = await _open("load", "S-OLD")
        try:
            assert agent.session_origin == "load"
            assert agent.session_id == "S-OLD"
            # stub 在 load 时回放 3 条历史
            assert agent.replayed_updates == 3
            assert received == []
            assert agent._loading is False
            # 回放窗口结束后，正常 update 必须还能转发出去
            await agent._dispatch({
                "method": "session/update",
                "params": {"sessionId": "S-OLD", "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "live"}}},
            })
            assert len(received) == 1
        finally:
            await _close(agent)
    asyncio.run(run())


def test_两个都不支持就新开():
    async def run():
        agent, _ = await _open("none", "S-OLD")
        try:
            assert agent.session_origin == "new"
            assert agent.session_id == "S-NEW"
        finally:
            await _close(agent)
    asyncio.run(run())


def test_恢复成功但回的是新会话不能算接回():
    """有些适配器会收下 resume、回 200，却给出一个完全不同的 sessionId。

    这时候标 "resume" 会让界面说「接回上次会话」，而它其实什么都不记得——
    用户据此以为它在看前文，比标错更糟。
    （codex / claude 不是这种：它们成功时**根本不回 sessionId**，见上一个用例。
    这条是防线，防的是协议里那种「静默换会话」的实现。）
    """
    async def run():
        agent, _ = await _open("resume-other-id", "S-OLD")
        try:
            assert agent.session_id == "S-FRESH"
            assert agent.session_origin == "new"
            # 原因也要留下来，界面上要能解释"为什么没接回"
            assert "另一个会话 id" in agent.resume_error
            # 会话是新的，但连接必须是活的
            res = await agent.prompt("还在吗", timeout=30)
            assert res.get("stopReason") == "end_turn"
        finally:
            await _close(agent)
    asyncio.run(run())


def test_恢复失败要把适配器原话留下来():
    """`no rollout found for thread id X` 和 `not logged in` 是两件事。

    前者说明那次会话本来就没落盘（没什么可接，别再去试同一个 id）；
    后者说明线程还在磁盘上，只是这次起不来，下次值得重试。
    只记 session_origin="new" 把这个区别抹掉了，出问题时谁也说不清为什么。
    """
    async def run():
        agent, _ = await _open("bad-resume", "S-OLD")
        try:
            assert agent.session_origin == "new"
            assert "no such session" in agent.resume_error
        finally:
            await _close(agent)
    asyncio.run(run())


def test_声明支持但恢复失败要退回新开且仍然能用():
    async def run():
        agent, _ = await _open("bad-resume", "S-OLD")
        try:
            assert agent.session_origin == "new"
            assert agent.session_id == "S-NEW"
            # 关键：恢复失败不能把 agent 变成死连接
            res = await agent.prompt("还在吗", timeout=30)
            assert res.get("stopReason") == "end_turn"
        finally:
            await _close(agent)
    asyncio.run(run())


def test_没有存过会话就老老实实新开():
    """第一次进房间时不该凭空猜一个 sessionId 去 load。"""
    async def run():
        agent, _ = await _open("resume", None)
        try:
            assert agent.session_origin == "new"
            assert agent.session_id == "S-NEW"
        finally:
            await _close(agent)
    asyncio.run(run())


# ---------- 会话映射的持久化 ----------

def test_会话映射存得下读得回(tmp_path):
    store = Store(tmp_path, "g2")
    assert store.load_sessions() == {}
    store.save_sessions({"pi": "S-1", "omp": "S-2"})
    assert Store(tmp_path, "g2").load_sessions() == {"pi": "S-1", "omp": "S-2"}
    # 临时文件不该留在目录里
    assert not list(store.dir.glob("*.tmp"))


def test_坏json当没有而不是炸掉(tmp_path):
    store = Store(tmp_path, "g2")
    store.save_sessions({"pi": "S-1"})
    store.sessions_path.write_text("{not json", encoding="utf-8")
    assert store.load_sessions() == {}


def test_清空历史必须连会话映射一起作废(tmp_path):
    """历史都留档了，还去 load 旧 session 就把两个人的上下文接串了。"""
    store = Store(tmp_path, "g2")
    store.save_sessions({"pi": "S-1"})
    store.clear()
    assert store.load_sessions() == {}
    assert list(store.dir.glob("sessions-*.json.bak"))
