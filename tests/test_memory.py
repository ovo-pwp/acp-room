"""G1 房间记忆 —— 讨论的汇总写下来，下一轮发回去。

分两层测：
  - `core/memory.py` 本体（纯文件 IO，可以脱离宿主跑）
  - 注入位置：记忆必须在身份之后、`---` 之前，跑到正文后面会被当成本轮议题
"""

from __future__ import annotations

import time

from core import context as ctx
from core.memory import Memory


def _mem(tmp_path, **kw) -> Memory:
    return Memory(tmp_path, "g1", **kw)


def test_没记过就不注入(tmp_path):
    """空记忆必须返回空串。返回一段"暂无"会让每个 agent 每轮多读一句废话。"""
    assert _mem(tmp_path).block() == ""


def test_空文本不占一条记忆(tmp_path):
    m = _mem(tmp_path)
    assert m.add("议题", "", by="hermes") == {}
    assert m.add("议题", "   \n  ", by="hermes") == {}
    assert m.rows() == []


def test_记下来读得回且新的在前(tmp_path):
    m = _mem(tmp_path)
    m.add("要不要上 Docker", "共识：先上，分歧在回滚方案", by="hermes")
    time.sleep(0.01)
    m.add("第二个议题", "共识：不做", by="pi")

    rows = m.rows()
    assert len(rows) == 2
    recent = m.recent(1)
    assert recent[0]["text"] == "共识：不做"
    assert recent[0]["by"] == "pi"
    assert m.recent(0) == list(reversed(rows))


def test_注入只给最近几条且每条截断(tmp_path):
    m = _mem(tmp_path, keep=2, snippet=20)
    for i in range(5):
        m.add(f"议题{i}", "很长" * 40 + str(i), by="hermes")

    block = m.block()
    assert block.count("- 议题") == 2, "只该注入最近 keep 条"
    assert "议题4" in block and "议题0" not in block
    assert "…" in block
    # 全文一条没丢，注入的只是截断版
    assert len(m.rows()) == 5


def test_注入块带默认与冲突的处理口径(tmp_path):
    m = _mem(tmp_path)
    m.add("议题", "共识：用 JSONL 而不是 SQLite", by="hermes")
    block = m.block()
    assert "[往期的结论]" in block
    assert "冲突" in block, "必须告诉 agent 撞上冲突时明说，而不是默默接受"
    assert "hermes 汇总" in block


def test_坏行跳过而不是把房间卡住(tmp_path):
    m = _mem(tmp_path)
    m.add("议题", "好的那条", by="pi")
    m.path.write_text(
        m.path.read_text(encoding="utf-8") + "{不是 json\n", encoding="utf-8")
    assert len(m.rows()) == 1


def test_清空留档不删(tmp_path):
    m = _mem(tmp_path)
    m.add("议题", "共识", by="pi")
    m.clear()
    assert m.rows() == []
    assert list(m.dir.glob("memory-*.jsonl.bak"))


# ---------- 注入位置 ----------

def _assemble(memory: str) -> str:
    plan = ctx.ContextPlan("本轮正文内容", 1, 0, 0)
    return ctx.assemble("房间规则", "", plan, note="", wait_hint="[WAIT 用法]",
                        self_id="pi", self_name="Pi",
                        roster=[("omp", "OMP")], memory=memory)


def test_记忆在分隔线之前正文之后():
    """身份块之后、`---` 之前是"背景"区。跑到正文后面会被当成本轮议题。"""
    out = _assemble("[往期的结论]\n- 共识：用 JSONL\n\n")
    assert out.index("[你是谁]") < out.index("[往期的结论]") < out.index("---") < out.index("本轮正文内容")


def test_没有记忆时拼装结果不变():
    """默认参数必须与改动前逐字节一致，否则每次动拼装都会悄悄改变所有 agent 的行为。"""
    assert _assemble("") == _assemble("[往期的结论]\n- 无关\n\n").replace(
        "[往期的结论]\n- 无关\n\n", "", 1)


def test_记忆不会挤掉wait提示():
    """wait 提示在最后是刻意的（离输出越近越容易被遵守）。"""
    out = _assemble("[往期的结论]\n- 共识\n\n")
    assert out.endswith("[WAIT 用法]")
