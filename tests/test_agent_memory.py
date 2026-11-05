"""G6 把结论推进 **agent 自己的**长期记忆文件。

这组测试全部跑在 tmp_path 造的家目录里，绝不碰真实的 ~/.claude、~/.codex、
Hermes 的 memories —— 那几个目录是别的程序在用的，测试无权写。

覆盖的是三类容易出事的地方：
  - 落点不存在时**不凭空建**（没装的 agent 要跳过并说清原因）
  - markdown 落点：块外的用户内容必须一字不改
  - Hermes store：条目格式 / 预算 / 漂移判定，和它自己的口径一致
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core import agent_memory as am
from core.agent_memory import ENTRY_DELIMITER, HERMES_MARK, MARK_BEGIN, MARK_END, AgentMemory


def _rows(*topics: str) -> list[dict]:
    """Memory.recent() 的形状：新的在前。"""
    base = time.time()
    return [{"ts": base - i, "topic": t, "text": f"共识：{t} 这么做", "by": "omp"}
            for i, t in enumerate(topics)]


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """一个假的 $HOME：只把 claude 的目录建出来，其余一概不存在。"""
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _am(home: Path, tmp_path: Path, **cfg) -> AgentMemory:
    return AgentMemory(tmp_path / "room", "g6", cfg=cfg, home=str(home))


# ---------- 落点 ----------

def test_没装的agent不凭空建目录(fake_home, tmp_path):
    """~/.codex 不存在 = Codex 没在这台机器上跑过。

    给它建一个家目录是很典型的越界：一个空的 ~/.codex 会让 Codex 自己
    以为配置好了，排查起来比少一份记忆麻烦得多。
    """
    m = _am(fake_home, tmp_path)
    t = m.target_for("codex")
    assert t.path is None and "Codex" in t.why
    out = m.promote(_rows("甲"), agents=["codex"])[0]
    assert out["status"] == "skipped"
    assert not (fake_home / ".codex").exists()


def test_未知id要老实承认不知道(fake_home, tmp_path):
    m = _am(fake_home, tmp_path)
    t = m.target_for("grok")
    assert t.path is None
    assert "需要你配" in t.why


def test_配置可以覆盖落点(fake_home, tmp_path):
    (fake_home / "elsewhere").mkdir()
    m = _am(fake_home, tmp_path, targets={"grok": "~/elsewhere/MEMORY.md"})
    t = m.target_for("grok")
    assert t.path == fake_home / "elsewhere" / "MEMORY.md"


# ---------- markdown 落点 ----------

def test_块外的用户内容一字不改(fake_home, tmp_path):
    """这是本模块最要紧的一条：那个文件首先是用户的。"""
    f = fake_home / ".claude" / "CLAUDE.md"
    user = "# 我的偏好\n\n- 回答要短\n- 不要用emoji\n"
    f.write_text(user, encoding="utf-8")

    m = _am(fake_home, tmp_path)
    m.promote(_rows("要不要上 Docker"), agents=["claude"])
    raw = f.read_text(encoding="utf-8")
    assert raw.startswith(user), "用户手写的前置内容被改了"
    assert MARK_BEGIN in raw and MARK_END in raw
    assert "要不要上 Docker" in raw

    # 第二次写：替换块内，不追加第二个块，用户内容仍在
    m.promote(_rows("第二个议题"), agents=["claude"])
    raw2 = f.read_text(encoding="utf-8")
    assert raw2.count(MARK_BEGIN) == 1
    assert raw2.startswith(user)
    assert "第二个议题" in raw2


def test_条目数受keep约束(fake_home, tmp_path):
    (fake_home / ".codex").mkdir()
    m = _am(fake_home, tmp_path, keep=2)
    rows = [{"ts": time.time() - i, "topic": f"议题{i}", "text": f"结论{i}", "by": "omp"}
            for i in range(6)][::-1]     # 新的在前
    out = m.promote(rows, agents=["codex"])[0]
    assert out["status"] == "written"
    entries = am.AgentMemory._markdown_entries((fake_home / ".codex" / "AGENTS.md").read_text(encoding="utf-8"))
    assert len(entries) == 2
    assert "议题5" in entries[0]        # 最老的那条排在最前（新的在前）


def test_父目录在就可以建文件(fake_home, tmp_path):
    """CLAUDE.md 本身不存在是正常状态（Claude Code 不强制它存在）。"""
    m = _am(fake_home, tmp_path)
    assert not (fake_home / ".claude" / "CLAUDE.md").exists()
    out = m.promote(_rows("议题"), agents=["claude"])[0]
    assert out["status"] == "written"
    assert (fake_home / ".claude" / "CLAUDE.md").exists()


def test_内容一致就不要再动文件(fake_home, tmp_path):
    """每次点按钮都重写一遍 = 每次留一份备份 + mtime 变化。
    别的程序（含 watcher）会因此误判"用户改了记忆"。
    """
    m = _am(fake_home, tmp_path)
    rows = _rows("同一个议题")
    assert m.promote(rows, agents=["claude"])[0]["status"] == "written"
    bak_dir = tmp_path / "room" / "data" / "g6" / "agent-memory"
    first = len(list(bak_dir.iterdir())) if bak_dir.exists() else 0
    out = m.promote(rows, agents=["claude"])[0]
    assert out["status"] == "unchanged"
    second = len(list(bak_dir.iterdir())) if bak_dir.exists() else 0
    assert second == first


def test_备份落在房间自己的目录里(fake_home, tmp_path):
    """agent 的目录是别人的地盘：往那儿扔 .bak 等于永久留垃圾。"""
    f = fake_home / ".claude" / "CLAUDE.md"
    f.write_text("x\n", encoding="utf-8")
    m = _am(fake_home, tmp_path)
    out = m.promote(_rows("议题"), agents=["claude"])[0]
    assert Path(out["backup"]).exists()
    assert str(tmp_path) in out["backup"]
    assert list((fake_home / ".claude").iterdir()) == [f]


# ---------- Hermes store ----------

@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hh"
    mem = home / "AppData" / "Local" / "hermes" / "memories"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("", encoding="utf-8")
    (home / "AppData" / "Local" / "hermes" / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 2200\n", encoding="utf-8")
    return home


def _hh_am(hermes_home, tmp_path, **cfg) -> AgentMemory:
    return AgentMemory(tmp_path / "r2", "g6", cfg=cfg, home=str(hermes_home))


def test_hermes写的条目按它的口径读得回(hermes_home, tmp_path):
    """Hermes 的解析是 split("\\n§\\n") + strip + 去空 + 去重。
    我们写出去的文件必须过同一个解析，否则它读进去的是半条。
    """
    m = _hh_am(hermes_home, tmp_path)
    f = hermes_home / "AppData" / "Local" / "hermes" / "memories" / "MEMORY.md"
    f.write_text("用户自己的一条" + ENTRY_DELIMITER + "另一条", encoding="utf-8")
    out = m.promote(_rows("议题A", "议题B"), agents=["hermes"])[0]
    assert out["status"] == "written", out

    entries = m._hermes_entries(f.read_text(encoding="utf-8"))
    assert entries[0] == "用户自己的一条" and entries[1] == "另一条"
    assert all(e == e.strip() for e in entries)
    ours = [e for e in entries if e.startswith(HERMES_MARK)]
    assert len(ours) == 2
    assert len(ENTRY_DELIMITER.join(entries)) <= 2200


def test_hermes预算不够时先丢我们自己的老条目(hermes_home, tmp_path):
    """腾地方只能腾自己的。删用户记的东西是不可逆的越界。"""
    f = hermes_home / "AppData" / "Local" / "hermes" / "memories" / "MEMORY.md"
    user = "U" * 1900
    stale = f"{HERMES_MARK} 旧结论：{'x' * 120}"
    f.write_text(user + ENTRY_DELIMITER + stale, encoding="utf-8")

    m = _hh_am(hermes_home, tmp_path, keep=6)
    rows = [{"ts": time.time() - i, "topic": f"议题{i}", "text": "Y" * 200, "by": "omp"}
            for i in range(5)][::-1]
    out = m.promote(rows, agents=["hermes"])[0]
    entries = m._hermes_entries(f.read_text(encoding="utf-8"))
    assert user in entries, "用户条目被动过"
    assert stale not in entries, "我们自己的老条目没被替换掉"
    assert len(ENTRY_DELIMITER.join(entries)) <= 2200
    if out["status"] == "written":
        assert any(e.startswith(HERMES_MARK) for e in entries)


def test_hermes自己的记忆已满就拒绝并说明(hermes_home, tmp_path):
    """预算是按**字**算的，不是字节（`ls` 看到 2,190 字节其实只有 1,379 字 ——
    汉字一个 3 字节，看错单位会让人以为 store 满了、于是永远不去写它）。
    真满了的时候硬塞会让 Hermes 之后每一次 add 都失败且不报原因，
    所以这时必须拒绝。
    """
    f = hermes_home / "AppData" / "Local" / "hermes" / "memories" / "MEMORY.md"
    full = "U" * 2201
    f.write_text(full, encoding="utf-8")
    before = f.read_text(encoding="utf-8")
    out = _hh_am(hermes_home, tmp_path).promote(_rows("议题"), agents=["hermes"])[0]
    assert out["status"] == "failed"
    assert "超了预算" in out["detail"] or "超过整份预算" in out["detail"]
    assert f.read_text(encoding="utf-8") == before


def test_hermes漂移文件不重写(hermes_home, tmp_path):
    """一条比整份预算还长的条目 = 有人往 store 里塞了自由文本。
    重写会把它丢掉，所以按 Hermes 自己的口径同样拒绝。
    """
    f = hermes_home / "AppData" / "Local" / "hermes" / "memories" / "MEMORY.md"
    drift = "Z" * 3000
    f.write_text(drift, encoding="utf-8")
    out = _hh_am(hermes_home, tmp_path).promote(_rows("议题"), agents=["hermes"])[0]
    assert out["status"] == "failed" and "§" in out["detail"]
    assert f.read_text(encoding="utf-8") == drift


def test_hermes空文件时正常写入(hermes_home, tmp_path):
    out = _hh_am(hermes_home, tmp_path).promote(_rows("议题甲"), agents=["hermes"])[0]
    assert out["status"] == "written"
    f = hermes_home / "AppData" / "Local" / "hermes" / "memories" / "MEMORY.md"
    assert f.read_text(encoding="utf-8").startswith(HERMES_MARK)


def test_hermes的memories目录不存在就跳过(tmp_path):
    home = tmp_path / "nohh"
    (home / "AppData" / "Local" / "hermes").mkdir(parents=True)
    m = AgentMemory(tmp_path / "r3", "g6", cfg={}, home=str(home))
    t = m.target_for("hermes")
    assert t.path is None and "memories" in t.why
    assert m.promote(_rows("议题"), agents=["hermes"])[0]["status"] == "skipped"


# ---------- 其它 ----------

def test_没有结论时谁都不写(fake_home, tmp_path):
    m = _am(fake_home, tmp_path)
    for o in m.promote([], agents=["claude", "codex", "hermes"]):
        assert o["status"] == "skipped" and "没有结论" in o["detail"]
    assert not (fake_home / ".claude" / "CLAUDE.md").exists()


def test_status报出现状(fake_home, tmp_path):
    m = _am(fake_home, tmp_path)
    m.promote(_rows("议题"), agents=["claude"])
    st = {s["agent"]: s for s in m.status()}
    assert st["claude"]["present"] is True
    assert st["claude"]["count"] == 1
    assert st["codex"]["exists"] is False


def test_summarize给人话():
    assert am.summarize([{"status": "written"}, {"status": "failed"}]) == "写入1、失败1"
    assert am.summarize([]) == "没有可写的落点"


# ---------- 房间接线（Room.promote_memory / Room.clear） ----------

import asyncio  # noqa: E402  放在这里是为了让上面那批纯文件测试不依赖 room

import room as room_mod  # noqa: E402
from core.audit import Audit  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.policy import Policy  # noqa: E402
from core.store import Store  # noqa: E402


class _Ag:
    session_id = "sess-1"


def _fake_room(tmp_path, home, rules=None, agents=("claude", "hermes")):
    """只装 promote_memory / clear 用到的那几样。

    `Room.__new__` 造的假房间缺一个属性就会在**别的**测试里炸
    （G5 加 self.spoke 时踩过），所以这里把用到的都补齐。
    """
    r = room_mod.Room.__new__(room_mod.Room)
    r.agents = {a: _Ag() for a in agents}
    r.seq = 7
    r._initiator = "person"
    r.store = Store(tmp_path / "store", "g6")
    r.audit = Audit(tmp_path / "store")
    r.memory = Memory(tmp_path / "store", "g6")
    r.agent_memory = AgentMemory(tmp_path / "store", "g6", cfg={},
                                 agent_ids=list(agents), home=str(home))
    # from_rules 而不是 Policy(config=...)：后者会把线上 config.json 的规则
    # 叠进来，于是"没规则就该拒"的用例在本地全绿、在别人机器上红。
    r.policy = Policy.from_rules(rules or {"default_action": "escalate"})
    r.transcript = []
    r.seen = {a: 0 for a in agents}
    r.saved_sessions = {}
    r.spoke = set()
    r.notices = []

    async def _announce(text):
        r.notices.append(text)

    async def _push():
        pass

    r.announce = _announce
    r.push_state = _push
    return r


def _rows2(*topics: str) -> list[dict]:
    base = time.time()
    return [{"ts": base - i, "topic": t, "text": f"共识：{t} 这么做", "by": "claude"}
            for i, t in enumerate(topics)]


def test_人按按钮时escalate不再二次拦但deny照样拦(tmp_path):
    """刚点完按钮又弹一张确认卡片 = 没听懂这次点击。
    但 auto_deny 是绝对的：路径规则对人也不许绕。
    """
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    r = _fake_room(tmp_path, home)
    r.memory.add("议题甲", "共识：这么做", by="claude")

    out = asyncio.run(r.promote_memory(agents=["claude"], initiator="person"))
    assert out[0]["status"] == "written"
    assert (home / ".claude" / "CLAUDE.md").exists()

    r2 = _fake_room(tmp_path, home, rules={"auto_deny": [{"tool": "agent_memory"}]})
    out2 = asyncio.run(r2.promote_memory(agents=["claude"], initiator="person"))
    assert out2[0]["status"] == "skipped" and "策略拒绝" in out2[0]["detail"]


def test_自动路径没人确认就跳过并留痕(tmp_path):
    """discussion 发起人 = 没人看着。escalate 必须落成"跳过 + 说明"，
    而不是静默不写：静默会让用户以为功能装了却没效果。
    """
    home = tmp_path / "h2"
    (home / ".claude").mkdir(parents=True)
    r = _fake_room(tmp_path, home)
    r.memory.add("议题乙", "共识：那么做", by="claude")

    out = asyncio.run(r.promote_memory(agents=["claude"], initiator="discussion"))
    assert out[0]["status"] == "skipped" and "没人确认" in out[0]["detail"]
    assert not (home / ".claude" / "CLAUDE.md").exists()
    row = r.audit.query(event="agent_memory")[-1]
    assert row["status"] == "skipped" and row["decision"] == "deny"
    assert "没人确认" in row["rule"]


def test_留痕一行一个agent带会话(tmp_path):
    home = tmp_path / "h3"
    (home / ".claude").mkdir(parents=True)
    r = _fake_room(tmp_path, home, agents=("claude",))
    r.memory.add("议题丙", "共识：照做", by="claude")
    asyncio.run(r.promote_memory(agents=["claude"]))
    rows = r.audit.query(event="agent_memory")
    assert len(rows) == 1
    assert rows[0]["agent"] == "claude"
    assert rows[0]["session"] == "sess-1"
    assert rows[0]["initiator"] == "person"
    assert rows[0]["decision"] == "escalate"      # 人点的按钮，判定仍是真实判定


def test_没有结论时不碰任何文件(tmp_path):
    home = tmp_path / "h4"
    (home / ".claude").mkdir(parents=True)
    r = _fake_room(tmp_path, home, agents=("claude",))
    out = asyncio.run(r.promote_memory(agents=["claude"]))
    assert out == []
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert any("还没有讨论结论" in n for n in r.notices)


def test_announce说的是一次后生效不是它已经记得(tmp_path):
    """写文件 ≠ 现在这场会话记得。措辞撒谎比功能缺失更糟。"""
    home = tmp_path / "h5"
    (home / ".claude").mkdir(parents=True)
    r = _fake_room(tmp_path, home, agents=("claude",))
    r.memory.add("议题丁", "共识：照做", by="claude")
    asyncio.run(r.promote_memory(agents=["claude"]))
    assert any("下一次会话" in n for n in r.notices)


def test_清空房间会收回自己写的那部分(tmp_path):
    """房间侧的转录和记忆都作废了，agent 侧却还"记得"一段没说过的话 ——
    表现是"明明清空了，它下次开会话还是揪着那段不放"。
    """
    home = tmp_path / "h6"
    (home / ".claude").mkdir(parents=True)
    r = _fake_room(tmp_path, home, agents=("claude",))
    r.memory.add("议题戊", "共识：照做", by="claude")
    asyncio.run(r.promote_memory(agents=["claude"]))
    f = home / ".claude" / "CLAUDE.md"
    assert MARK_BEGIN in f.read_text(encoding="utf-8")
    f.write_text("# 用户自己的\n\n- 回答要短\n" + f.read_text(encoding="utf-8"), encoding="utf-8")

    asyncio.run(r.clear())
    raw = f.read_text(encoding="utf-8")
    assert MARK_BEGIN not in raw and "议题戊" not in raw
    assert "回答要短" in raw, "收回自己的块时把用户内容也带走了"

