"""审计落盘的字段的真实性。

这些测试存在的理由是一个很具体的事故：`Audit._normalize()` 返回的是**固定白名单**，
不在名单里的 kwarg 会被静默丢掉。而 room 的接缝测试清一色用 `class Audit: write(**kw)`
的假对象，假对象把 kwargs 原样存下来，于是「session= 传了但没落盘」这件事
在测试全绿的情况下活了很久。

所以这个文件只干一件事：用真的 Audit 写，再从磁盘读回来，断言字段还在。
"""

import json

import pytest

from core.audit import Audit


@pytest.fixture()
def audit(tmp_path):
    return Audit(tmp_path)


def _rows(path):
    return [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_session字段必须真的落盘(audit):
    """每条留痕都要能回答「这是哪次 ACP 会话产出的」。

    sessionId 一变，agent 侧上下文就清零，正文却照样顺——只有这个字段能区分。
    """
    audit.write(agent="omp", turn_seq=12, event="turn_wait",
                tool="prompt", title="等回复", status="ok",
                decision="allow", rule="超时前等待",
                initiator="discussion", session="sess-abcdef123")

    row = _rows(audit.path)[0]
    assert row["session"] == "sess-abcdef123"
    assert row["initiator"] == "discussion"
    assert row["event"] == "turn_wait"


def test_没有会话的记录留空串而不是缺键(audit):
    """schema 必须稳定：人发言没有 sessionId，字段也要在，值才是空串。

    否则读端要写两套（`row.get("session")` vs 键存在判断），而查询界面迟早踩。
    """
    audit.write(agent="person", event="notice", title="阶段切换", status="ok",
                decision="allow", rule="广播")
    row = _rows(audit.path)[0]
    assert "session" in row
    assert row["session"] == ""
    assert row["initiator"] == "person"


def test_汇总留痕在真实审计里带着会话和发起方(tmp_path):
    """把 G1 的 remember() 接到**真** Audit 上跑一遍。

    两个曾经同时坏掉的点：
      1. session= 被 _normalize 丢掉；
      2. initiator 读 self._initiator —— `_one_turn` 在 finally 里已经把它
         还原成 "person"，汇总在其之后写，于是留痕声称是人触发的。
    """
    import room as room_mod

    class A:
        session_id = "omp-sess-001"

    class Memory:
        def add(self, topic, text, by=""):
            return {"topic": topic, "text": text, "by": by}

        def rows(self):
            return [{"topic": "要不要上 Docker"}]

    r = room_mod.Room.__new__(room_mod.Room)
    r.agents = {"omp": A()}
    r.memory = Memory()
    r.audit = Audit(tmp_path)
    r.seq = 33
    r._initiator = "person"          # 模拟 one_turn 归还后的状态

    room_mod.Room.remember(r, "要不要上 Docker", "共识：用 JSONL", by="omp")

    row = _rows(r.audit.path)[0]
    assert row["event"] == "memory_write"
    assert row["session"] == "omp-sess-001"
    assert row["initiator"] == "discussion"
    assert row["agent"] == "omp"
    assert row["turn_seq"] == 33
