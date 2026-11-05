"""对话落盘：JSONL 追加写 + 载入恢复 + 导出 markdown。

为什么是 JSONL 而不是 SQLite：
- 每条记录独立一行，断电最多丢最后一行，不会把整个库写坏；
- 人可以直接 `tail -f` 看，也可以 `grep`；
- 不用装/迁移/加锁，一个文件就是一份完整历史。

一个房间一个目录：

    data/<room>/
        meta.json       房间元信息（创建时间、cwd、当时的 agent 名单）
        turns.jsonl     一条发言一行
        export.md       导出结果（由 export_md 生成）
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Store:
    def __init__(self, root: Path, room: str = "main") -> None:
        self.dir = root / "data" / room
        self.dir.mkdir(parents=True, exist_ok=True)
        self.turns_path = self.dir / "turns.jsonl"
        self.meta_path = self.dir / "meta.json"
        self._fh = None

    # ---------- 元信息 ----------

    def write_meta(self, info: dict) -> None:
        """只在房间第一次创建时写，不覆盖已有记录。"""
        if self.meta_path.exists():
            return
        payload = {"created": _now_iso(), **info}
        self.meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def read_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---------- 追加 ----------

    def append(self, turn) -> None:
        """turn 是 room.Turn（有 __dict__）。立即 flush，宁可慢也不丢。"""
        if self._fh is None:
            self._fh = open(self.turns_path, "a", encoding="utf-8", newline="\n")
        row = dict(turn.__dict__)
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass  # 某些文件系统不支持，忽略

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    # ---------- 载入 ----------

    def load(self) -> list[dict]:
        """读回全部记录。坏行跳过而不是炸掉整个文件。"""
        if not self.turns_path.exists():
            return []
        rows = []
        for line in self.turns_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def clear(self) -> None:
        self.close()
        if self.turns_path.exists():
            # 不删，改名留档。误点清空不该等于永久销毁。
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.turns_path.rename(self.dir / f"turns-{stamp}.jsonl.bak")
        # 会话映射也要一起作废：历史都留档了，还去 load 旧 session 就串了。
        if self.sessions_path.exists():
            self.sessions_path.rename(self.dir / f"sessions-{time.strftime('%Y%m%d-%H%M%S')}.json.bak")

    # ---------- agent 侧 ACP sessionId ----------

    @property
    def sessions_path(self) -> Path:
        return self.dir / "sessions.json"

    def save_sessions(self, mapping: dict) -> None:
        """agent id -> sessionId。先写临时文件再换名，避免半截文件。"""
        tmp = self.sessions_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.sessions_path)

    def load_sessions(self) -> dict:
        if not self.sessions_path.exists():
            return {}
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 导出 ----------

    def export_md(self, path: Path | None = None, title: str = "") -> Path:
        rows = self.load()
        meta = self.read_meta()
        out = path or (self.dir / "export.md")

        lines = [f"# {title or ('对话记录 ' + meta.get('created', ''))}", ""]
        if meta.get("cwd"):
            lines.append(f"- 工作目录：`{meta['cwd']}`")
        if meta.get("agents"):
            names = ", ".join(a.get("name", a.get("id", "?")) for a in meta["agents"])
            lines.append(f"- 参与：{names}")
        lines.append(f"- 条数：{len(rows)}")
        lines.append("")

        for r in rows:
            ts = r.get("ts")
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
            name = r.get("name", r.get("speaker", "?"))
            badge = []
            # 模型名放最前：复盘「这句为什么这么蠢」时第一眼要找的就是它。
            if r.get("model"):
                badge.append(f"model={r['model']}")
            if r.get("tokens"):
                badge.append(f"{r['tokens']} tok")
            if r.get("ms"):
                badge.append(f"{r['ms'] / 1000:.1f}s")
            # sessionId 前缀：导出稿里也能看出「这条和上一条不是同一个会话」。
            if r.get("session"):
                badge.append(f"sess {str(r['session'])[:8]}")
            if r.get("status") and r["status"] != "ok":
                badge.append(r["status"])
            suffix = f"  ·  {' / '.join(badge)}" if badge else ""
            lines.append(f"## {name}  ·  {when}{suffix}")
            lines.append("")
            lines.append((r.get("text") or "").rstrip())
            lines.append("")

        out.write_text("\n".join(lines), encoding="utf-8")
        return out


def safe_name(text: str, limit: int = 40) -> str:
    """把任意标题变成能当文件名的串。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", text).strip(" -.")
    return (cleaned[:limit] or "room")
