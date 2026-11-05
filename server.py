"""平台后端：FastAPI + WebSocket。

    python server.py            # http://127.0.0.1:8770
    python server.py --port 9000 --reload-agents

只做三件事：把房间状态推给前端、收前端指令、把指令翻译成 ACP 调用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.agent_admin import AgentError, remove_agent, probe, upsert_agent, validate_spec  # noqa: E402
from core.agent_admin import PRESETS as AGENT_PRESETS  # noqa: E402
from core.attachments import AttachmentError  # noqa: E402
from core.gui_log import log  # noqa: E402
# 别名是因为端点函数自己叫 perf()，直接 `from core import perf` 会被遮住。
from core import perf as perf_  # noqa: E402
from core.model_probe import ModelProber  # noqa: E402
from room import Room  # noqa: E402

ROOM = Room(ROOT)

_PROBER = ModelProber(ROOT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log(f"启动：cwd={ROOM.cwd}，agent={list(ROOM.agents)}")

    async def warm():
        # 后台连接，别堵住 HTTP 服务；npx 首次拉包可能要几十秒
        await asyncio.sleep(0.3)
        await ROOM.connect_all()

    task = asyncio.create_task(warm())
    yield
    task.cancel()
    await ROOM.close()


app = FastAPI(title="ACP Room", lifespan=lifespan)

# 静态文件：web/ 目录下的 css/js/图片。index.html 由下面路由单独返回。
# 注意：StaticFiles 默认不发 Cache-Control，浏览器就按"修改时间×10%"启发式缓存，
# 前端改完刷新看不到（2026-11-03 实际踩到：p3-skills.js 改了半小时还显示旧版）。
# 这里统一加 no-cache —— 带 etag 回源，没变就是 304，不会拖慢。
class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", NoCacheStatic(directory=str(ROOT / "web")), name="static")


class SayBody(BaseModel):
    text: str
    speakers: list[str] | None = None
    rounds: int = 1
    auto: bool = True       # 说完之后是否让 agents 自动轮流回
    images: list[dict] = []          # [{id}]，先经 /api/upload 落盘

class AgentBody(BaseModel):
    id: str
    name: str = ""
    cmd: list[str] | str = []
    color: str = ""
    model_hint: str = ""
    permission: str = "allow"
    # 人格三件套。留空就不写进 config.json ——既保留 core/persona.py 的内置默认，
    # 也不会在"重加一个已有 agent"时把它原来的人格抹掉（upsert 是合并语义）。
    role: str = ""
    tag: str = ""
    style: str = ""


class ProbeBody(BaseModel):
    cmd: list[str] | str = []

class RunBody(BaseModel):
    speakers: list[str] | None = None
    rounds: int = 1
    note: str = ""


class PermBody(BaseModel):
    id: str
    optionId: str


class DiscussBody(BaseModel):
    topic: str
    method: str = "standard"
    max_rounds: int = 4
    max_tokens: int = 200_000
    speakers: list[str] | None = None


class OptionBody(BaseModel):
    agent: str
    configId: str
    value: str


class PromoteBody(BaseModel):
    agents: list[str] | None = None


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state() -> JSONResponse:
    return JSONResponse(ROOM.snapshot())


@app.post("/api/say")
async def say(body: SayBody) -> JSONResponse:
    # 图片已经通过 /api/upload 落过盘，这里只带 id。取不到的 id 静默丢掉并记日志，
    # 不让一条坏 id 把整条消息拒掉——文字本身仍然有价值。
    atts = []
    for a in (body.images or []):
        aid = str((a or {}).get("id") or "")
        if aid and ROOM.attachments.path_of(aid):
            atts.append({**a, "id": aid, "url": f"/api/attachment/{aid}"})
        elif aid:
            log(f"[say] 附件 {aid} 不存在，已忽略")
    await ROOM.say_human(body.text, attachments=atts)
    if body.auto:
        ROOM.start_background(body.speakers, body.rounds, note="")
    await ROOM.push_state()
    return JSONResponse({"ok": True})


@app.post("/api/run")
async def run(body: RunBody) -> JSONResponse:
    ROOM.start_background(body.speakers, body.rounds, body.note)
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def stop() -> JSONResponse:
    ROOM.running = False
    for agent in ROOM.agents.values():
        asyncio.create_task(agent.cancel())
    await ROOM.push_state()
    return JSONResponse({"ok": True})


@app.post("/api/permission")
async def permission(body: PermBody) -> JSONResponse:
    ok = ROOM.resolve_permission(body.id, body.optionId)
    return JSONResponse({"ok": ok})


@app.post("/api/reconnect")
async def reconnect() -> JSONResponse:
    asyncio.create_task(ROOM.connect_all())
    return JSONResponse({"ok": True})


@app.post("/api/discuss")
async def discuss(body: DiscussBody) -> JSONResponse:
    """走阶段协议跑一场讨论，而不是简单轮流说话。"""
    from core.discussion import Budget

    ROOM.disc.method = body.method
    ROOM.disc.budget = Budget(max_rounds=body.max_rounds, max_tokens=body.max_tokens)
    speakers = [a for a in (body.speakers or ROOM.order) if a in ROOM.agents]

    async def go() -> None:
        await ROOM.say_human(body.topic)
        await ROOM.disc.run(body.topic, speakers)

    if ROOM.running:
        return JSONResponse({"ok": False, "error": "已经在跑了"}, status_code=409)
    asyncio.create_task(go())
    return JSONResponse({"ok": True, "speakers": speakers, "method": body.method})


@app.get("/api/options")
async def options() -> JSONResponse:
    return JSONResponse(ROOM.opts.snapshot())


@app.post("/api/options")
async def set_option(body: OptionBody) -> JSONResponse:
    agent = ROOM.agents.get(body.agent)
    if not agent:
        return JSONResponse({"ok": False, "error": "没有这个 agent"}, status_code=404)
    # 正在发言时切换会和 prompt 抢同一个会话（set_config_option 是会话级操作），
    # 直接拒掉，让前端等这一轮结束。
    if agent.busy:
        return JSONResponse({"ok": False, "error": f"{body.agent} 正在发言，等这一轮结束再切"},
                            status_code=409)
    try:
        entry = await ROOM.opts.set_option(agent, body.configId, body.value)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                            status_code=500)
    await ROOM.push_state()
    return JSONResponse({"ok": True, "entry": entry})


@app.get("/api/models/probe")
async def models_probe(agent: str = "codex") -> JSONResponse:
    """P14 批量模型真伪巡检。并发探测 agent 的所有模型选项，缓存 5 分钟。"""
    opts = ROOM.opts.for_agent(agent)
    model_entry = opts.model_entry
    models: list[str] = []
    if model_entry:
        models = [o["value"] for o in (model_entry.get("options") or []) if o.get("value")]
    result = await _PROBER.probe(agent, models)
    return JSONResponse(result)


@app.get("/api/skills")
async def skills() -> JSONResponse:
    return JSONResponse({"catalog": ROOM.skills.catalog(),
                         "broken": ROOM.skills.broken_count})


@app.get("/api/policy")
async def policy() -> JSONResponse:
    """当前生效的策略表 + 命中计数。自检用：闸门到底在不在跑，看这里。"""
    return JSONResponse(ROOM.policy.effective_config())


@app.post("/api/policy/reload")
async def policy_reload() -> JSONResponse:
    """改完 config.json 的 policy 块后不用重启。坏规则以 warnings 返回。"""
    ROOM.policy.configure()
    return JSONResponse({"ok": True, "policy": ROOM.policy.effective_config()})


@app.get("/api/agents")
async def agents() -> JSONResponse:
    return JSONResponse([
        {"id": s.id, "name": s.name, "cmd": s.cmd, "model_hint": s.model_hint,
         "permission": s.permission, "color": s.color}
        for s in ROOM.specs.values()
    ])


@app.get("/api/agent-presets")
async def agent_presets() -> JSONResponse:
    """常见 ACP agent 的一键预设。命令抄自 vendor/acpx/src/agent-registry.ts。"""
    return JSONResponse(AGENT_PRESETS)


@app.post("/api/upload")
async def upload(body: dict) -> JSONResponse:
    """收一张图，落盘，返回 {id, mime, bytes, url}。

    前端先上传拿到 id，再在 /api/say 里带上 images:[{id}]。
    分两步是为了：正文与图片分离（turns.jsonl 里永远只存 id），
    以及同一张图重传时按内容哈希自动去重。
    """
    data = body.get("data") or ""
    name = str(body.get("name") or "")
    if not data:
        return JSONResponse({"ok": False, "error": "没有收到图片内容"}, status_code=400)
    try:
        info = ROOM.attachments.save(data, name)
    except AttachmentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log(f"[upload] {info['id']} {info['mime']} {info['bytes']}B")
    return JSONResponse({"ok": True, **info})


@app.get("/api/attachment/{aid}")
async def attachment(aid: str) -> Response:
    """按 id 取原图。id 只接受 16 位十六进制，路径由 id + 白名单扩展名拼出。"""
    got = ROOM.attachments.read(aid)
    if not got:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw, mime = got
    return Response(content=raw, media_type=mime,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/agents/probe")
async def agents_probe(body: ProbeBody) -> JSONResponse:
    """只探活，不落地。前端"测试连接"按钮用。

    单独起一次性进程，不碰房间里正在用的实例——探活不能干扰正在进行的对话。
    """
    try:
        spec = validate_spec({"id": "probe", "cmd": body.cmd})
    except AgentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    result = await probe(spec["cmd"], ROOM.cwd)
    return JSONResponse(result)


@app.post("/api/agents")
async def agents_add(body: AgentBody) -> JSONResponse:
    """添加/更新一个 agent。顺序：校验 -> 探活 -> 写 config.json -> 注册进房间。

    先探活再写文件：一个写错的命令如果先进了 config.json，下次重启整个房间都起不来，
    而人只会在日志里看到一行 Traceback。
    """
    try:
        spec = validate_spec(body.model_dump())
    except AgentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    if spec["id"] in ROOM.specs:
        old = ROOM.specs[spec["id"]]
        same = (old.cmd == spec["cmd"] and old.name == spec["name"]
                and old.permission == spec["permission"])
        if same:
            return JSONResponse({"ok": True, "action": "unchanged",
                                 "agent": {"id": spec["id"], "name": spec["name"]}})

    check = await probe(spec["cmd"], ROOM.cwd)
    if not check.get("ok"):
        return JSONResponse({"ok": False, "error": check.get("error", "探活失败"),
                             "probe": check}, status_code=400)

    try:
        action = upsert_agent(spec)
        room_action = await ROOM.add_agent(spec)
    except (AgentError, OSError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "action": room_action, "file": action,
                         "probe": check,
                         "agent": {"id": spec["id"], "name": spec["name"]}})


@app.delete("/api/agents/{aid}")
async def agents_remove(aid: str) -> JSONResponse:
    """摘掉一个 agent：先从房间移除进程，再从 config.json 删掉。

    顺序反过来（先删配置）会留下一个跑着的孤儿进程。
    """
    ok = await ROOM.remove_agent(aid)
    if not ok:
        return JSONResponse({"ok": False, "error": f"房间里没有 {aid}"}, status_code=404)
    try:
        remove_agent(aid)
    except AgentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "removed": aid})


@app.get("/api/history")
async def history(limit: int = 0) -> JSONResponse:
    rows = ROOM.store.load()
    if limit > 0:
        rows = rows[-limit:]
    return JSONResponse({"count": len(rows), "turns": rows})


@app.get("/api/perf")
async def perf(refresh: int = 0) -> JSONResponse:
    """各家的速度读数（中位数 / p90 / 最长），按**全体**算。

    为什么需要这个端点：界面上原来只报每家「最长一轮 Xs」，而那只是一个最大值。
    实测按全体 416 条算，Codex 的最长那轮 87.2s 只排第 124 位，它的中位数 26.7s
    反而比 Pi（30.3s）还快——「谁拖慢了房间」这个问题，拿 max 回答会答反。

    发言（turn_end）不写审计，所以光读审计会**系统性漏掉发言**：实测 416 条
    发言行只剩 53 条。`refresh=1` 时顺带读一次 turns.jsonl 把发言补进来
    （只在这个面板打开时读，不在每次状态广播时读）。
    """
    rows = ROOM.audit.query(limit=5000)
    if refresh:
        rows = rows + ROOM.store.load()
    # 端点名是 perf，所以这里不能写 `"agents": summarize(...)`——那会让
    # 前端拿到 data.agents.agents，两个同名键指向不同的东西（外层是读数汇总，
    # 内层才是每家一档）。名字分开：summary / per_agent。
    summary = perf_.summarize(rows, ROOM.snapshot().get("perf_agent_ids") or [])
    return JSONResponse({
        "summary": summary,
        "total_rows": len(rows),
        # 有 ttft_ms 的行数。老记录没这个字段，界面用它说明「首字」是从哪条记录开始的。
        "with_ttft": sum(1 for r in rows if r.get("ttft_ms")),
        "turns_ms": sum(1 for r in rows if r.get("event") == "turn_end" and r.get("ms")),
    })


@app.get("/api/memory")
async def memory(limit: int = 0) -> JSONResponse:
    """房间记住了哪些结论（G1）。只读 —— 写它的唯一入口是讨论的汇总阶段。

    为什么要露出来：注入是每个 agent 每轮都发生的事，出了偏差（记错、记串、
    该忘的没忘）从对话正文里根本看不出来，只能看它记住了什么。
    """
    rows = ROOM.memory.recent(limit)
    return JSONResponse({"count": len(rows), "entries": rows,
                         "injected_now": ROOM.memory.block(),
                         # G6：房间这份之外，各 agent 自己那份的现状。
                         # 每个落点的 path 必须回显：写进了哪个文件，
                         # 不看路径等于让人猜（而且各家落点根本不一样）。
                         "agent_memory": {
                             "auto": ROOM.agent_memory.auto,
                             "targets": ROOM.agent_memory.status()}})


@app.post("/api/memory/promote")
async def memory_promote(body: PromoteBody) -> JSONResponse:
    """G6：把房间的结论推进每个 agent 自己的长期记忆文件。

    这是**人按的按钮**，所以发起人固定是 person：闸门只拦 deny 类规则，
    不再向人二次要确认（刚点完按钮又弹一张卡片，等于没听懂这次点击）。
    自动路径由 `agent_memory.auto_promote` 控制，走完整闸门。

    生效时机是各 agent 的**下一次会话** —— instructions / memory store 在
    新会话加载，现在这场里那个活的 ACP session 不会因为写了文件就改变。
    """
    unknown = [a for a in (body.agents or []) if a not in ROOM.specs]
    if unknown:
        return JSONResponse({"ok": False, "error": f"名册里没有：{'、'.join(unknown)}"},
                            status_code=400)
    try:
        out = await ROOM.promote_memory(agents=body.agents or None, initiator="person")
    except Exception as exc:       # noqa: BLE001 - 写别人的文件失败要回话，不能 500 一片空白
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "results": out})


@app.get("/api/bench")
async def bench() -> JSONResponse:
    """算力基准：每个 agent 所用模型的吞吐（tok/s）与上下文。

    立刻返回缓存，过期就顺便起一个后台刷新——绝不在请求里等（一次基准要十几秒）。
    首次调用可能返回空 rows + probing=true，前端下轮轮询就有数。
    """
    b = getattr(ROOM, "bench", None)
    if b is None:
        return JSONResponse({"enabled": False, "rows": [], "probing": False,
                             "hint": "bench 未接入"})
    b.ensure_background()
    return JSONResponse(b.snapshot())


@app.get("/api/health")
async def health() -> JSONResponse:
    """一屏看清房间到底什么状态。

    为什么需要：这些信息原本分散在 /api/state、/api/options、/api/audit、
    /api/agents 和三个日志文件里，要挨个 curl 才能拼出一张图。
    更关键的是，它会把“配置与实际不一致”这件事直接摆出来——
    比如 permission 写着 ask 但从来没生效过、mode 被限制在只读档。
    """
    import statistics

    turns = ROOM.transcript
    agents_out = []
    for aid in ROOM.order:
        spec = ROOM.specs[aid]
        ag = ROOM.agents[aid]
        mine = [t for t in turns if t.speaker == aid]
        ms = [t.ms for t in mine if t.ms]
        toks = [t.tokens for t in mine if t.tokens]
        # 当前配置项（mode / model / thinking 等）
        cur = {}
        try:
            snap = ROOM.opts.for_agent(aid)
            for it in snap.items:
                cur[it["id"]] = it.get("currentValue")
        except Exception:  # noqa: BLE001
            pass
        agents_out.append({
            "id": aid,
            "name": spec.name,
            "color": spec.color,
            "connection": ROOM.status.get(aid, "未知"),
            "permission": spec.permission,
            "model_hint": spec.model_hint,
            "config": cur,
            "turns": len(mine),
            "last_ms": mine[-1].ms if mine else 0,
            "avg_ms": int(statistics.mean(ms)) if ms else 0,
            "p50_ms": int(statistics.median(ms)) if ms else 0,
            "max_ms": max(ms) if ms else 0,
            "tokens_total": sum(toks),
            "busy": bool(getattr(ag, "busy", False)),
            "errors": sum(1 for t in mine if t.status != "ok"),
        })

    # 审计
    aud = {"enabled": False}
    au = _audit()
    if au is not None:
        try:
            aud = {"enabled": True, **au.stats()}
            rows = au.query(limit=1)
            aud["last_ts"] = rows[0].get("ts") if rows else None
        except Exception as exc:  # noqa: BLE001
            aud = {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

    # 讨论
    d = ROOM.disc
    disc = {
        "running": ROOM.running,
        "method": getattr(d, "method", "?"),
        "tokens_used": getattr(d, "tokens_used", 0),
        "rounds_traced": len(getattr(d, "trace", []) or []),
        "max_rounds": getattr(getattr(d, "budget", None), "max_rounds", 0),
    }

    # 数据落盘
    store_dir = ROOM.store.dir
    files = {}
    for name in ("turns.jsonl", "audit.jsonl", "meta.json"):
        p = store_dir / name
        files[name] = p.stat().st_size if p.exists() else 0

    # 汇总告警：把真问题摆到明面上
    warnings = []
    for a in agents_out:
        if a["connection"] != "在线":
            warnings.append(f"{a['name']} 未连接：{a['connection']}")
        if a["errors"]:
            warnings.append(f"{a['name']} 有 {a['errors']} 轮非正常结束")
        if a["max_ms"] > 120000:
            warnings.append(f"{a['name']} 最慢一轮 {a['max_ms'] // 1000}s")
    if aud.get("enabled") and aud.get("total", 0) == 0:
        warnings.append("审计已接入但 0 条记录（可能从未触发工具调用）")
    if not aud.get("enabled"):
        warnings.append("审计未接入（core/audit.py 缺失）")

    return JSONResponse({
        "cwd": ROOM.cwd,
        "room": ROOM.store.dir.name,
        "running": ROOM.running,
        "turns": len(turns),
        "agents": agents_out,
        "audit": aud,
        "discussion": disc,
        "files": files,
        "warnings": warnings,
    })


@app.get("/api/export")
async def export() -> JSONResponse:
    p = ROOM.export_markdown()
    return JSONResponse({"ok": True, "path": str(p), "bytes": p.stat().st_size})


@app.get("/api/export.md")
async def export_md_download():
    from fastapi.responses import FileResponse

    p = ROOM.export_markdown()
    return FileResponse(p, media_type="text/markdown", filename=p.name)


@app.post("/api/clear")
async def clear() -> JSONResponse:
    await ROOM.clear()
    return JSONResponse({"ok": True})


# ---------- P6 审计接缝（pi 接，codex 只负责 core/audit.py）----------
#
# P6 交付前后都可能有人调这两个端点，所以一律走 getattr 降级：
#   ROOM.audit 不存在 → 404 + enabled=false，前端显示占位而不是白屏。
# 这样 P7（claude 的前端）可以先做，不必等我接完。

def _audit():
    """拿到 P6 的 Audit 实例；没有就返回 None（不是抛异常）。"""
    return getattr(ROOM, "audit", None)


@app.get("/api/audit")
async def audit_query(limit: int = 200, agent: str | None = None,
                      event: str | None = None, since: float | None = None) -> JSONResponse:
    au = _audit()
    if au is None:
        return JSONResponse({"enabled": False, "rows": [], "stats": {},
                             "hint": "P6 审计未接入 core/audit.py"})
    try:
        rows = au.query(limit=limit, agent=agent, event=event, since=since)
    except TypeError:
        # codex 的实现若没吃全参数，退化成只按 limit 查，别 500
        rows = au.query(limit=limit)
    return JSONResponse({"enabled": True, "count": len(rows), "rows": rows,
                         "stats": au.stats()})


@app.get("/api/audit/export.md")
async def audit_export_md():
    from fastapi.responses import FileResponse

    au = _audit()
    if au is None:
        return JSONResponse({"ok": False, "error": "P6 审计未接入"}, status_code=404)
    p = au.export_md()
    return FileResponse(p, media_type="text/markdown", filename=p.name)


# ---------- P5 重置接缝（pi 接，claude 只负责 core/config_opts.py）----------

class ResetBody(BaseModel):
    agent: str
    configId: str


@app.post("/api/options/reset")
async def options_reset(body: ResetBody) -> JSONResponse:
    """清掉 state.json 里该 agent 的这一项，回到 agent 自己的默认值。

    配合 POST /api/reconnect 使用：清完重连，agent 会用它的原生默认。
    这是「用户点错了彻底回不去」的逃生口（见 P5）。
    """
    if body.agent not in ROOM.agents:
        return JSONResponse({"ok": False, "error": "没有这个 agent"}, status_code=404)
    fn = getattr(ROOM.opts, "reset_pref", None)
    if fn is None:
        return JSONResponse({"ok": False, "error": "P5 reset_pref 未实现"},
                            status_code=501)
    try:
        fn(body.agent, body.configId)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                            status_code=500)
    return JSONResponse({"ok": True, "agent": body.agent, "configId": body.configId,
                         "hint": "再调 POST /api/reconnect 让 agent 用回原生默认"})


@app.websocket("/ws")
async def ws(sock: WebSocket) -> None:
    await sock.accept()
    ROOM.sockets.add(sock)
    try:
        await sock.send_text(json.dumps(ROOM.snapshot(), ensure_ascii=False))
        while True:
            raw = await sock.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "say":
                await ROOM.say_human(msg.get("text") or "")
                if msg.get("auto", True):
                    ROOM.start_background(msg.get("speakers"), int(msg.get("rounds") or 1))
                await ROOM.push_state()
            elif kind == "run":
                ROOM.start_background(msg.get("speakers"), int(msg.get("rounds") or 1),
                                      msg.get("note") or "")
            elif kind == "stop":
                ROOM.running = False
                for agent in ROOM.agents.values():
                    asyncio.create_task(agent.cancel())
            elif kind == "permission":
                ROOM.resolve_permission(msg.get("id"), msg.get("optionId"))
            elif kind == "order":
                order = [a for a in (msg.get("order") or []) if a in ROOM.agents]
                ROOM.order = order or ROOM.order
                await ROOM.push_state()
            elif kind == "discuss":
                from core.discussion import Budget

                ROOM.disc.method = msg.get("method") or "standard"
                ROOM.disc.budget = Budget(
                    max_rounds=int(msg.get("max_rounds") or 4),
                    max_tokens=int(msg.get("max_tokens") or 200_000),
                )
                spk = [a for a in (msg.get("speakers") or ROOM.order) if a in ROOM.agents]
                if ROOM.running:
                    await sock.send_text(json.dumps(
                        {"type": "notice", "text": "已经在跑了，先停止再说"},
                        ensure_ascii=False))
                else:
                    async def go(topic=msg.get("topic") or "", s=spk):
                        await ROOM.say_human(topic)
                        await ROOM.disc.run(topic, s)
                    asyncio.create_task(go())
            elif kind == "option":
                agent = ROOM.agents.get(msg.get("agent"))
                if agent and agent.busy:
                    await sock.send_text(json.dumps(
                        {"type": "notice",
                         "text": f"{msg.get('agent')} 正在发言，等这一轮结束再切模型"},
                        ensure_ascii=False))
                elif agent:
                    try:
                        await ROOM.opts.set_option(agent, msg.get("configId"),
                                                   msg.get("value"))
                        await ROOM.push_state()
                    except Exception as exc:  # noqa: BLE001
                        await sock.send_text(json.dumps(
                            {"type": "notice", "text": f"切换失败：{exc}"}, ensure_ascii=False))
            elif kind == "clear":
                await ROOM.clear()
            elif kind == "export":
                p = ROOM.export_markdown()
                await sock.send_text(json.dumps(
                    {"type": "exported", "path": str(p), "bytes": p.stat().st_size},
                    ensure_ascii=False))
            elif kind == "ping":
                await sock.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"ws 异常: {type(exc).__name__}: {exc}")
    finally:
        ROOM.sockets.discard(sock)


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8770")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
