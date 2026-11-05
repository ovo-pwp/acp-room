"""算力基准 —— 实测每个 agent 所用模型的吞吐与上下文。

## 为什么需要它

P16 的算力面板要显示「tok/s」和「上下文」两列，但这两个数**不在 `/api/health` 里**：
health 只知道「这个 agent 跑了多久」，不知道「这个模型有多快」。

而且我实测过一组数据（2026-11-03）：

    deepseek-v4.1-flash    73.4 tok/s
    haiku                 131.9 tok/s   ← 最快
    spark-x2.6             73.7 tok/s
    GLM-5.3-Flash          59.1 tok/s   ← 最慢

**但它们答题能力几乎一样**（都答对了 13/27 这类题）。耗时却差 10 倍——
差在推理档位，不在模型。这个结论必须让用户在界面上直接看到。

## 为什么端点写在配置里，而不是去解析各 agent 的配置文件

agent 的端点分散在四种格式里：pi 用 `models.json`、omp 用 `models.yml`、
codex 用 `config.toml`、claude 用 `settings.json`。解析它们既脆弱又容易**测错对象**——
我自己就踩过一次：拿 3065 的模型列表去比对 omp（它实际连 `api.deepseek.com`），
得出「模型不存在」的错误结论。

所以这里用**显式声明**：`config.json` 的 `bench.targets`。测错时看得见，而不是静默测错。

## 与主流程的关系

**绝不阻塞。** 一次完整基准要十几秒（四个并发请求），所以：

    GET /api/bench
      → 立刻返回上次缓存（可能为空）+ {"probing": true|false}
      → 若缓存过期，顺便在后台起一个刷新任务

首次调用返回空行，前端显示 "—"，几秒后下一次轮询就有数了。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from core.gui_log import log

DEFAULT_PROMPT = "写一段关于分布式系统的技术说明，400字左右，纯段落，不要标题不要列表。"

# 一次基准的默认参数。写死在这里而不是配置里，是因为它们不是用户调优项，
# 是"让不同模型可比"的控制变量——改了就不可比了。
DEFAULT_MAX_TOKENS = 900
DEFAULT_TIMEOUT = 120
DEFAULT_CACHE = 600          # 秒


async def _http_post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    """POST JSON，返回解析后的 dict。用 asyncio.to_thread 包住同步 urllib——

    这样不引入 aiohttp/httpx 依赖（项目只装了 fastapi 自带的 httpx，
    但为了一处调用再拉一条依赖链不划算）。"""
    import urllib.request
    import urllib.error

    def _do() -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.to_thread(_do)


async def _measure(target: dict, prompt: str, max_tokens: int,
                   timeout: float) -> dict:
    """测一个目标。返回 {agent, tok_s, ctx, took_ms, error, model}。"""
    agent = target.get("agent") or "?"
    proto = (target.get("protocol") or "openai").lower()
    base = (target.get("baseUrl") or "").rstrip("/")
    model = target.get("model") or ""
    ctx = target.get("ctx")            # 配置里可以直接写死，省一次查询
    out: dict[str, Any] = {"agent": agent, "model": model, "ctx": ctx,
                           "tok_s": None, "took_ms": 0, "error": None}

    if not base or not model:
        out["error"] = "缺少 baseUrl 或 model"
        return out

    headers = {}
    # key 两种给法：直接写 `key`（本地自用），或 `keyEnv` 指环境变量（不进仓库）。
    # 刻意不支持从各 agent 的配置文件里现读——那是四种格式，既脆弱又容易测错对象。
    key = target.get("key")
    if not key and target.get("keyEnv"):
        key = os.environ.get(str(target["keyEnv"])) or ""
    if key:
        headers["Authorization"] = "Bearer " + str(key)
    if not key and target.get("keyEnv"):
        out["error"] = f"环境变量 {target['keyEnv']} 未设置"
        return out
    try:
        if proto == "anthropic":
            # claude 走 /v1/messages，output_tokens 在 usage 里
            url = base + "/messages" if base.endswith("/v1") else base + "/v1/messages"
            headers["anthropic-version"] = "2023-06-01"
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            t0 = time.time()
            d = await _http_post(url, body, headers, timeout)
            el = time.time() - t0
            u = d.get("usage") or {}
            ntok = u.get("output_tokens") or 0
        else:
            # openai chat-completions（pi/omp/codex 的网关都走这条）
            url = base + "/chat/completions"
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            t0 = time.time()
            d = await _http_post(url, body, headers, timeout)
            el = time.time() - t0
            u = d.get("usage") or {}
            ntok = u.get("completion_tokens") or 0
    except Exception as exc:  # noqa: BLE001 —— 任何一个目标挂掉都不能拖垮整轮
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return out

    out["took_ms"] = int(el * 1000)
    if ntok and el > 0:
        out["tok_s"] = round(ntok / el, 1)
    elif ntok == 0:
        out["error"] = "上游没返回 token 用量"
    return out


class Bench:
    """基准结果的持有者。Room 持有一个。"""

    def __init__(self, root: Path, room: str = "main",
                 config: dict | None = None) -> None:
        cfg = dict(config or {})
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.prompt: str = cfg.get("prompt") or DEFAULT_PROMPT
        self.max_tokens: int = int(cfg.get("max_tokens") or DEFAULT_MAX_TOKENS)
        self.timeout: float = float(cfg.get("timeout") or DEFAULT_TIMEOUT)
        self.cache_seconds: int = int(cfg.get("cache_seconds") or DEFAULT_CACHE)
        self.targets: list[dict] = list(cfg.get("targets") or [])

        self.path = root / "data" / room / "bench.json"
        self._rows: list[dict] = []
        self._ts: float = 0.0
        self._probing: bool = False
        self._task: asyncio.Task | None = None
        self._load()

    # ---------- 缓存 ----------

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = list(d.get("rows") or [])
            self._ts = float(d.get("ts") or 0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log(f"[bench] 读缓存失败（{type(exc).__name__}: {exc}），从空开始")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": self._ts, "rows": self._rows},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log(f"[bench] 写缓存失败：{exc}")     # 缓存写不了不影响读数

    # ---------- 查询 ----------

    @property
    def stale(self) -> bool:
        return (time.time() - self._ts) > self.cache_seconds

    def snapshot(self) -> dict:
        """给前端的形状：{rows:[{agent, tok_s, ctx, ...}], probing, ts, age_s, targets}。

        `rows` 为空且 `probing=true` 表示"第一次测，几秒后再来"。
        """
        return {
            "rows": self._rows,
            "probing": self._probing,
            "ts": self._ts,
            "age_s": int(time.time() - self._ts) if self._ts else None,
            "stale": self.stale,
            "targets": len(self.targets),
            "enabled": self.enabled,
        }

    # ---------- 刷新 ----------

    def ensure_background(self) -> None:
        """缓存过期就起一个后台刷新。绝不在请求线程里等。"""
        if not self.enabled or not self.targets:
            return
        if self._probing or self._task is not None:
            return
        if not self.stale:
            return
        # 先置位再起任务：create_task 只是排队，refresh() 要等一次事件循环才跑起来。
        # 不先置位的话，本次响应会报 probing=false + rows 为空，前端会误判成“没在测”。
        self._probing = True
        try:
            self._task = asyncio.create_task(self._refresh_and_clear())
        except RuntimeError:
            # 没有运行中的事件循环（比如 CLI 单跑）——回退，别炸
            self._probing = False
            log("[bench] 无事件循环，跳过后台刷新")

    async def _refresh_and_clear(self) -> None:
        """后台任务入口：_probing 由 ensure_background 置位，这里直接干活。"""
        try:
            await self._run()
        finally:
            self._task = None

    async def refresh(self) -> list[dict]:
        """对外入口：带重入保护。内部逻辑在 _run()。

        注意：ensure_background() 会先置 _probing=True 再调度，所以这里不能
        再把 _probing 当入口卫兵用（那样会立刻返回、永远卡在 probing）。
        """
        if self._probing:
            return self._rows
        return await self._run()

    async def _run(self) -> list[dict]:
        """并发测所有目标，写缓存。单个目标失败不影响其它。"""
        self._probing = True
        t0 = time.time()
        try:
            results = await asyncio.gather(
                *(_measure(t, self.prompt, self.max_tokens, self.timeout)
                  for t in self.targets),
                return_exceptions=True,
            )
            rows = []
            for t, r in zip(self.targets, results):
                if isinstance(r, BaseException):
                    rows.append({"agent": t.get("agent") or "?", "model": t.get("model") or "",
                                 "ctx": t.get("ctx"), "tok_s": None, "took_ms": 0,
                                 "error": f"{type(r).__name__}: {str(r)[:100]}"})
                else:
                    if r.get("ctx") is None and t.get("ctx") is not None:
                        r["ctx"] = t["ctx"]
                    rows.append(r)
            self._rows = rows
            self._ts = time.time()
            self._save()
            ok = sum(1 for r in rows if r.get("tok_s"))
            log(f"[bench] 刷新完成 {ok}/{len(rows)} 个目标可测，"
                f"耗时 {time.time() - t0:.1f}s")
            return rows
        finally:
            self._probing = False
