"""房间长期记忆 —— 把「讨论定下来的东西」存下来，下一轮还给 agent。

## 为什么需要

G2 之前，每次重启所有 agent 都是全新会话，连上一场讨论定了什么都没人记得；
G2 之后 pi/omp/hermes 能接回自己的会话，但 **claude 和 codex 的 adapter 接不回**
（实测：claude 报 `-32002 Resource not found`，codex 报 `no rollout found for thread id`）。
也就是说：房间这一侧的记忆（transcript）一直都有，agent 那一侧的记忆是残缺的。

结论写回是补这个缺口的最小办法：一场讨论走完汇总阶段之后，那段
「共识 / 分歧 / 下一步」是全场信息密度最高的产物，让它只活在一轮对话里
等于每次讨论都从零开始。

## 刻意不做的事

- **不做 LLM 摘要**。汇总阶段本身已经是摘要了（`core/convergence.py` 的 SYNTHESIS），
  再调一次模型只是把「摘要出错」这种查不出来的失真叠加上去。
- **不按 agent 分文件**。房间是共享的，结论属于房间。分人存会出现
  「pi 记得但 omp 不记得」这种更难查的状态。
- **不自动遗忘**。条数上限是硬截断（丢最老的），不做打分/合并——
  合并需要模型，回到上一条。

## 注入的长度为什么卡这么死

每条只回注 200 字、最多 3 条：约 600 字 ≈ 300 token，每个 agent 每轮都带。
5 个 agent × 每轮 × 1000 token 是很贵的，而「记得上次结论」值不了这个价。
真要细看，全文在 `data/<room>/memory.jsonl` 里，一条没删。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Sequence

# 注入时每条截断长度
SNIPPET = 200
# 注入最近几条
KEEP = 3

_HEADER = (
    "[往期的结论]\n"
    "以下是这个房间此前讨论定下来的结论（按时间从近到远）。"
    "**默认它们仍然成立**；如果这一轮的议题和某条冲突，明确说出冲突在哪，"
    "不要默默接受也不要默默推翻。"
)


class Memory:
    """`data/<room>/memory.jsonl` 的读写。一个房间一份，所有 agent 共享。"""

    def __init__(self, root: Path, room: str = "main",
                 keep: int = KEEP, snippet: int = SNIPPET) -> None:
        self.dir = Path(root) / "data" / room
        self.path = self.dir / "memory.jsonl"
        self.keep = keep
        self.snippet = snippet

    # ---------- 写 ----------

    def add(self, topic: str, text: str, by: str = "") -> dict:
        """记一条。空文本不记（汇总失败时不该留下一条空白记忆）。"""
        text = (text or "").strip()
        if not text:
            return {}
        row = {"ts": time.time(), "topic": (topic or "").strip()[:120],
               "text": text, "by": by}
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass    # 某些文件系统不支持，和 store.py 一样忽略
        return row

    def clear(self) -> None:
        """不删，改名留档 —— 和 Store.clear 同一个理由。"""
        if self.path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.path.rename(self.dir / f"memory-{stamp}.jsonl.bak")

    # ---------- 读 ----------

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue    # 坏行跳过，别把整个房间卡住
            if isinstance(r, dict) and (r.get("text") or "").strip():
                out.append(r)
        return out

    def recent(self, limit: int = 0) -> list[dict]:
        """新的在前。limit<=0 表示全部。"""
        rows = list(reversed(self.rows()))
        return rows[:limit] if limit > 0 else rows

    # ---------- 注入 ----------

    def block(self) -> str:
        """给 prompt 的那一段。没有记忆就返回空串，不占位置。"""
        rows = self.recent(self.keep)
        if not rows:
            return ""
        lines = [_HEADER]
        for r in rows:
            one = " ".join((r.get("text") or "").split())
            if len(one) > self.snippet:
                one = one[:self.snippet] + "…"
            topic = (r.get("topic") or "").strip()
            who = (r.get("by") or "").strip()
            head = f"- 议题「{topic}」" if topic else "- "
            if who:
                head += f"（{who} 汇总）"
            lines.append(f"{head} {one}")
        return "\n".join(lines) + "\n\n"
