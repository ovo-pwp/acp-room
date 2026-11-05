"""审计落盘：JSONL 追加写 + 查询 + 统计 + Markdown 导出。

为什么和 turns.jsonl 一样用 JSONL：
- 每条记录独立一行，断电最多丢最后一行；
- 人可以直接 tail -f / grep；
- 不用建表、迁移或锁库。

故障隔离是硬约束：write 失败只 log，不向上抛，主流程继续。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from core.gui_log import log


class _WriteResult:
    """让同一个 write 既能 await，也能在同步 resolve_permission 里直接调用。"""

    def __await__(self):
        if False:
            yield None
        return None


class Audit:
    def __init__(self, root: Path, room: str = "main") -> None:
        self.dir = Path(root) / "data" / room
        self.path = self.dir / "audit.jsonl"
        self._lock = threading.Lock()
        self._seq: int | None = None
        self._pid_agent: dict[str, str] = {}
        self._pid_turn: dict[str, int] = {}

    def write(self, **fields: Any) -> _WriteResult:
        """写一条审计记录。异步 await 或同步调用都可以，失败永不抛。"""
        try:
            self._append(fields)
        except Exception as exc:
            log(f"[audit] write failed: {exc}")
        return _WriteResult()

    def _append(self, fields: dict[str, Any]) -> None:
        row = self._normalize(fields)
        with self._lock:
            seq = self._seq if self._seq is not None else self._last_seq() + 1
            row["seq"] = seq
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            self._seq = seq + 1

    def _normalize(self, fields: dict[str, Any]) -> dict[str, Any]:
        fields = dict(fields)
        user_option = fields.pop("user_option", None)
        pid = fields.pop("pid", None)

        event = fields.get("event") or "tool_call"
        agent = fields.get("agent")
        turn_seq = fields.get("turn_seq")

        if pid:
            agent = agent or self._pid_agent.get(pid, "unknown")
            turn_seq = turn_seq if turn_seq is not None else self._pid_turn.get(pid, 0)

        if event == "permission" and pid:
            if agent:
                self._pid_agent[pid] = str(agent)
            if turn_seq is not None:
                self._pid_turn[pid] = int(turn_seq)

        if user_option is not None:
            decision = "user_denied" if user_option == "__cancel__" else "user_allowed"
            rule = fields.get("rule") or f"user selected {user_option}"
        else:
            decision = fields.get("decision") or "auto_allow"
            rule = fields.get("rule") or "unknown"
        status = fields.get("status") or "pending"
        if event == "tool_result" and status == "in_progress":
            status = "pending"
        elif event == "tool_result" and status == "completed":
            status = "ok"
        elif event == "tool_result" and status == "failed":
            status = "error"

        return {
            "ts": float(fields.get("ts") or time.time()),
            "agent": str(agent or "unknown"),
            "turn_seq": int(turn_seq or 0),
            "event": str(event),
            "tool": str(fields.get("tool") or "unknown"),
            "title": str(fields.get("title") or ""),
            "status": str(status),
            "decision": str(decision),
            "rule": str(rule),
            "initiator": str(fields.get("initiator") or "person"),
            "ms": int(fields.get("ms") or 0),
            # 产出这条记录的 ACP 会话。必须在这里显式保留：_normalize 是白名单，
            # 不在名单里的 kwarg 会被静默丢掉——曾经 session= 就是这么消失的，
            # 于是「每条发言都留痕」变成「留痕里看不出是哪次会话」。
            # 上下文是否在某一轮被清零，只有这个字段能回答。
            "session": str(fields.get("session") or ""),
        }

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = max(last, int(json.loads(line).get("seq", 0)))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except OSError:
            return last
        return last

    def query(
        self,
        limit: int = 200,
        agent: str | None = None,
        event: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        rows = self._read()
        if agent is not None:
            rows = [r for r in rows if r.get("agent") == agent]
        if event is not None:
            rows = [r for r in rows if r.get("event") == event]
        if since is not None:
            since_ts = float(since)
            rows = [r for r in rows if float(r.get("ts", 0)) >= since_ts]
        if limit <= 0:
            return []
        return rows[-limit:]

    def stats(self) -> dict:
        rows = self._read()
        return {
            "total": len(rows),
            "by_agent": dict(Counter(r.get("agent", "unknown") for r in rows)),
            "by_decision": dict(Counter(r.get("decision", "unknown") for r in rows)),
            "errors": sum(1 for r in rows if r.get("status") == "error"),
        }

    def export_md(self, title: str = "审计日志") -> Path:
        rows = self._read()
        stats = self.stats()
        lines = [f"# {title}", ""]
        lines.append(f"- 条数：{stats['total']}")
        lines.append(f"- 按 agent：{json.dumps(stats['by_agent'], ensure_ascii=False)}")
        lines.append(f"- 按 decision：{json.dumps(stats['by_decision'], ensure_ascii=False)}")
        lines.append(f"- 错误：{stats['errors']}")
        lines.append("")
        for row in rows:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.get("ts", 0)))
            lines.append(
                f"## {row.get('seq')} · {when} · {row.get('agent')} · "
                f"{row.get('event')} · {row.get('decision')}"
            )
            lines.append("")
            lines.append(json.dumps(row, ensure_ascii=False, indent=2))
            lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = self.path.with_name("audit.md")
        out.write_text("\n".join(lines), encoding="utf-8")
        return out

    def close(self) -> None:
        pass

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            log(f"[audit] read failed: {exc}")
            return rows
        return rows
