"""批量模型真伪巡检（P14）。并发探测网关上所有模型是否真的可完成一次最小请求。

P5 的 _probe_model 只在切换时单点探测一个模型；这里做批量并发巡检，
结果缓存 5 分钟，接口先返回缓存 + probing 标记，不阻塞请求线程。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.gui_log import log


@dataclass
class ModelProbeResult:
    agent: str
    checked: int
    reachable: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    took_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "checked": self.checked,
            "reachable": self.reachable,
            "dead": self.dead,
            "took_ms": self.took_ms,
        }


class ModelProber:
    """并发巡检某个 agent 的所有模型选项。结果缓存 CACHE_SECONDS 秒。"""

    CACHE_SECONDS = 300

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[float, ModelProbeResult]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _probe_config(self) -> dict:
        cfg = {
            "enabled": True,
            "baseUrl": os.environ.get("ACP_ROOM_PROBE_BASE") or "",
            "timeout": 5,
        }
        try:
            raw = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
            p = raw.get("probe")
            if isinstance(p, dict):
                if isinstance(p.get("enabled"), bool):
                    cfg["enabled"] = p["enabled"]
                if isinstance(p.get("baseUrl"), str):
                    cfg["baseUrl"] = p["baseUrl"].strip()
                try:
                    t = float(p.get("timeout"))
                    if t > 0:
                        cfg["timeout"] = t
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
        return cfg

    async def _probe_one(self, model: str, cfg: dict) -> tuple[bool | None, str | None]:
        base = str(cfg.get("baseUrl") or "").strip().rstrip("/")
        if not base:
            return None, None
        url = f"{base}/chat/completions"
        try:
            import httpx  # 项目 .venv 自带
            async with httpx.AsyncClient(timeout=float(cfg.get("timeout") or 5)) as client:
                r = await client.post(url, json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                })
            if 200 <= r.status_code < 300:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                if isinstance(body, dict) and body.get("error"):
                    return False, f"HTTP {r.status_code}: {body['error']}"
                return True, None
            return False, f"HTTP {r.status_code}: {r.text[:200].strip()}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    async def _run_probe(self, agent: str, models: list[str]) -> ModelProbeResult:
        cfg = self._probe_config()
        t0 = time.time()
        if not cfg.get("enabled") or not str(cfg.get("baseUrl") or "").strip():
            result = ModelProbeResult(agent=agent, checked=0,
                                      took_ms=int((time.time() - t0) * 1000))
            self._cache[agent] = (time.time(), result)
            return result
        results = await asyncio.gather(
            *[self._probe_one(m, cfg) for m in models],
            return_exceptions=True,
        )
        reachable: list[str] = []
        dead: list[str] = []
        for m, res in zip(models, results):
            if isinstance(res, BaseException):
                dead.append(m)
                continue
            ok, _err = res
            if ok is True:
                reachable.append(m)
            else:
                dead.append(m)
        result = ModelProbeResult(agent=agent, checked=len(models),
                                  reachable=reachable, dead=dead,
                                  took_ms=int((time.time() - t0) * 1000))
        self._cache[agent] = (time.time(), result)
        log(f"[model_probe] {agent} checked={len(models)} reachable={len(reachable)} dead={len(dead)} took={result.took_ms}ms")
        return result

    async def probe(self, agent: str, models: list[str]) -> dict[str, Any]:
        """返回缓存结果；若缓存过期/无缓存则启动后台探测并返回 probing=true。"""
        cached = self._cache.get(agent)
        if cached and (time.time() - cached[0]) < self.CACHE_SECONDS:
            result = cached[1]
            return {**result.as_dict(), "probing": False}
        task = self._tasks.get(agent)
        if task and not task.done():
            if cached:
                result = cached[1]
                return {**result.as_dict(), "probing": True}
            return {"agent": agent, "checked": 0, "reachable": [], "dead": [],
                    "took_ms": 0, "probing": True}
        # 启动新的后台探测
        task = asyncio.create_task(self._run_probe(agent, models))
        self._tasks[agent] = task
        if cached:
            result = cached[1]
            return {**result.as_dict(), "probing": True}
        return {"agent": agent, "checked": 0, "reachable": [], "dead": [],
                "took_ms": 0, "probing": True}
