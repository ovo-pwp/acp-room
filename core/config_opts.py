"""配置项发现与切换 —— 【P1 归 claude：模型与配置选择器】

背景（PROJ/RESEARCH.md 实测）：四个 ACP agent 在 `session/new` 的响应里带
`configOptions`，其中 `category == "model"` 的完整列表合计 982 个模型
（pi 359 / omp 604 / claude 5 / codex 10），平台此前只打日志，一个都没用上。
切换走 `session/set_config_option`，三个 agent 均已实测生效。

本文件职责：
1. `normalize()`：把各 agent 不统一的返回结构归一成同一种（标准 configOptions /
   老字段 models.availableModels / omp 的 models:null 都吃下）。每组带一个
   `via`（config|legacy）——它决定切换走 `session/set_config_option`
   还是老适配器才有的 `session/set_model`。
2. `ConfigOpts`：单个 agent 的配置项集合（查询、当前值、快照）。
3. `ConfigStore`：所有 agent 的配置项 + 切换 + 持久化。
   持久化分两层：`config.json` 里每个 agent 的 `"model"` / `"options"` 字段是静态基线
   （pi 的骨架文件，本模块只读不写）；`state.json` 是运行时覆盖——用户手点/命令行切换
   写这里，启动时在 `discover()` 里自动应用。选 state.json 而不是回写 config.json 的
   理由：config.json 是结构配置、四个项目都会碰，运行时状态单独成文件才不会互相踩。
   合并规则：state.json 覆盖 config.json 同名项。
4. P5 新增（修"切了回不去"）：切换后用并集合并（options 旧∪新、消失的标
   `stale:true`）而不是整体覆盖；`ConfigStore.reset_pref()` 删除 state.json 单条
   预置；模型切换成功后对探测网关发一条轻量请求，结果记 `reachable` /
   `reachable_error`（探测失败不阻塞切换）。

接口签名不许改（room.py / server.py 依赖）：
    normalize(raw) -> list[dict]
    ConfigOpts(agent_id)
        .update(raw) / .options() / .model_entry / .model / .find(config_id) / .snapshot()
    ConfigStore()
        .for_agent(agent_id) / .discover(agent, raw=None) / .set_option(agent, config_id, value)
        .model_of(agent_id) / .snapshot()
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.gui_log import log

if TYPE_CHECKING:
    from core.agent import AcpAgent

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state.json"

# P5 探测默认值（pi 裁定 0010）：baseUrl 不硬编码任何端口——15721 是 CC Switch
# 第三方代理的端口，随时可能换。认环境变量 ACP_ROOM_PROBE_BASE；
# config.json 的 "probe" 块（若 pi 加了）覆盖本默认。
# 空 baseUrl = 跳过探测（reachable=None），不算失败。
PROBE_DEFAULT: dict[str, Any] = {
    "enabled": True,
    "baseUrl": os.environ.get("ACP_ROOM_PROBE_BASE") or "",
    "timeout": 3,
}


def normalize(raw: dict) -> list[dict]:
    """把各 agent 返回的不统一结构，归一成同一种。

    三种情况都要吃下：
    1. 标准 configOptions（四个 agent 都有）
    2. 老字段 models.availableModels（pi / codex 额外带，omp 一开始是 null）
    3. 只有 modes（没有模型可切，比如某些只读 agent）

    防御：options 里混进裸字符串时包成 {"value": ...}。
    """
    out: list[dict] = []
    seen: set[str] = set()

    for opt in raw.get("configOptions") or []:
        oid = opt.get("id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append({
            "id": oid,
            "name": opt.get("name") or oid,
            "category": opt.get("category") or "other",
            "type": opt.get("type") or "select",
            "via": "config",
            "currentValue": opt.get("currentValue"),
            "description": opt.get("description"),
            "options": [
                {
                    "value": o["value"] if isinstance(o, dict) else o,
                    "name": (o.get("name") if isinstance(o, dict) else None) or
                            (o["value"] if isinstance(o, dict) else o),
                    "description": o.get("description") if isinstance(o, dict) else None,
                }
                for o in (opt.get("options") or [])
                if (o.get("value") if isinstance(o, dict) else o) is not None
            ],
        })

    # 老字段兜底：configOptions 里没 model，但 models.availableModels 有
    models = raw.get("models") or {}
    avail = models.get("availableModels") or []
    if avail and not any(o["category"] == "model" for o in out):
        out.append({
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            # via="legacy" 决定后面走哪个 ACP 方法：这类适配器只实现了
            # session/set_model（ACP 0.25 之前的模型口），不吃 configId。
            "via": "legacy",
            "currentValue": models.get("currentModelId"),
            "description": "来自 models.availableModels（老字段）",
            "options": [
                {"value": m.get("modelId") or m.get("value"),
                 "name": m.get("name") or m.get("modelId") or m.get("value"),
                 "description": m.get("description")}
                for m in avail
                if m.get("modelId") or m.get("value")
            ],
        })

    return out


class ConfigOpts:
    """一个 agent 的配置项集合。每个 agent 一个实例。"""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.items: list[dict] = []

    def update(self, raw: dict) -> None:
        """把 session/new 或 set_config_option 的返回值吃进来。"""
        self.items = normalize(raw)
        models = self.model_entry
        if models:
            log(f"[{self.agent_id}] 配置项 {len(self.items)} 组，"
                f"模型 {len(models['options'])} 个，当前 {models['currentValue']}")

    def merge_from(self, raw: dict) -> dict | None:
        """P5：把 set_config_option 的返回值按并集合进本地快照（修"切了回不去"）。

        原则：
        - currentValue 以返回值为准（这是切没切成的唯一证据）
        - options 取并集：旧列表 ∪ 新列表，按 value 去重；新值追加，消失的旧值
          保留并标 stale:true（agent 给的列表是"当前上下文下的推荐"，不是唯一
          合法值——历史选过的值必须永远可回退）
        - 返回值里没有的其它配置项不动（不整体覆盖，防止列表缩水）
        """
        incoming = normalize(raw)
        last: dict | None = None
        for new in incoming:
            old = self.find(new["id"])
            if old is None:
                self.items.append(new)
                last = new
                continue
            merged = dict(old)
            merged["currentValue"] = new.get("currentValue")
            for k in ("name", "category", "type", "description"):
                if new.get(k) is not None:
                    merged[k] = new[k]
            by_value: dict[str, dict] = {
                o["value"]: dict(o) for o in (old.get("options") or [])
            }
            new_opts = new.get("options") or []
            new_values: set[str] = set()
            for o in new_opts:
                v = o["value"]
                new_values.add(v)
                if v in by_value:
                    keep = by_value[v]
                    keep.pop("stale", None)
                    for k in ("name", "description"):
                        if o.get(k):
                            keep[k] = o[k]
                else:
                    by_value[v] = {k: val for k, val in o.items() if val is not None}
            cur = merged.get("currentValue")
            if new_opts:  # 响应真的给了列表，才把消失的旧值标 stale
                for v, o in by_value.items():
                    if v not in new_values and v != cur:
                        o["stale"] = True
                    else:
                        o.pop("stale", None)
            if cur is not None and cur not in by_value:
                by_value[cur] = {"value": cur, "name": cur}
            merged["options"] = list(by_value.values())
            self.items[self.items.index(old)] = merged
            last = merged
        return last

    def options(self) -> list[dict]:
        return self.items

    @property
    def model_entry(self) -> dict | None:
        for o in self.items:
            if o.get("category") == "model":
                return o
        return None

    @property
    def model(self) -> str | None:
        e = self.model_entry
        return e["currentValue"] if e else None

    def find(self, config_id: str) -> dict | None:
        for o in self.items:
            if o.get("id") == config_id:
                return o
        return None

    def snapshot(self) -> dict:
        model = self.model_entry
        return {
            "model": self.model,
            "modelCount": len(model["options"]) if model else 0,
            "options": self.items,
        }


class ConfigStore:
    """所有 agent 的配置项。Room 持有一个。

    持久化（两层合并，state 覆盖 config）：
      config.json  → agents[i].model / agents[i].options     静态基线（只读）
      state.json   → {"<agent>": {"model": "...", "options": {...}}}  运行时覆盖
    """

    def __init__(self, root: Path | None = None) -> None:
        self.by_agent: dict[str, ConfigOpts] = {}
        self.root = root or ROOT
        self.state_file = self.root / "state.json"

    def for_agent(self, agent_id: str) -> ConfigOpts:
        if agent_id not in self.by_agent:
            self.by_agent[agent_id] = ConfigOpts(agent_id)
        return self.by_agent[agent_id]

    def model_of(self, agent_id: str) -> str:
        """当前生效的模型 id。没发现过就返回空串，**绝不**为查询创建条目——
        人类发言也走这里，凭空建一个 "human" 条目会混进 snapshot()/brief()，
        界面上多出一个没有模型的假 agent。"""
        opts = self.by_agent.get(agent_id)
        if opts is None:
            return ""
        cur = opts.model
        return str(cur) if cur else ""

    async def discover(self, agent: "AcpAgent", raw: dict | None = None,
                       apply_prefs: bool = True) -> None:
        """agent 连上后调用。raw 不传就自己发 session/new 拿。

        apply_prefs=True（room 启动路径）时，发现完自动应用持久化预置
        （config.json 静态 + state.json 运行时覆盖）。CLI 的 --discover 传 False，
        诊断不产生副作用。
        """
        opts = self.for_agent(agent.cfg.id)
        if raw is not None:
            opts.update(raw)
        else:
            # 已连接但没拿到 configOptions 的场景：重新开一个会话取一次
            res = await agent.request("session/new", {
                "cwd": agent.cfg.cwd, "mcpServers": [],
            })
            opts.update(res)
        if apply_prefs:
            await self._apply_prefs(agent)

    async def set_option(self, agent: "AcpAgent", config_id: str, value: str) -> dict:
        """切换。返回更新后的该组配置（带 verified 字段），用 currentValue 确认真的切了。

        校验：config_id 不存在或 value 不在可选项里 → 抛 ValueError（fail fast，
        别等远端报一个说不清的错）。切换成功写 state.json。
        """
        opts = self.for_agent(agent.cfg.id)
        if not opts.items:
            # 还没发现过（理论上的兜底路径，room 在 connect 时已发现）
            await self.discover(agent, apply_prefs=False)
        entry = opts.find(config_id)
        if entry is None:
            known = [o["id"] for o in opts.items]
            raise ValueError(f"[{agent.cfg.id}] 没有配置项 {config_id!r}，"
                             f"现有 {known}")
        valid = [o["value"] for o in entry.get("options") or []]
        if valid and value not in valid:
            raise ValueError(
                f"[{agent.cfg.id}] {config_id} 不接受 {value!r}："
                f"可选 {len(valid)} 个，例如 {valid[:5]}")

        if entry.get("via") == "legacy":
            # 只实现了老口子的适配器：session/set_model 不收 configId，
            # 返回值也是空的，所以「切没切成」只能取「这一枪没报错」。
            # 这是那类适配器能给的全部信息，别指望更多。
            await agent.request("session/set_model", {
                "sessionId": agent.session_id,
                "modelId": value,
            })
            entry = opts.find(config_id)
            if entry is not None:
                entry["currentValue"] = value
        else:
            res = await agent.request("session/set_config_option", {
                "sessionId": agent.session_id,
                "configId": config_id,
                "value": value,
            })
            # P5：不再 opts.update(res) 整体覆盖。set_config_option 返回的列表是
            # "当前上下文下的推荐"，直接覆盖会把切换前本地全量列表丢掉（"切了回不去"）。
            # 改成并集合并：currentValue 以返回值为准，options 旧∪新、消失的标 stale。
            opts.merge_from(res)
            entry = opts.find(config_id)
        verified = bool(entry) and entry.get("currentValue") == value
        if not verified:
            log(f"[{agent.cfg.id}] 切换 {config_id}={value} 未确认，"
                f"返回 currentValue={entry.get('currentValue') if entry else None}")
        else:
            log(f"[{agent.cfg.id}] {config_id} -> {value}")
            self.save_pref(agent.cfg.id, config_id, value)
        if entry is not None:
            entry = dict(entry)
            entry["verified"] = verified
            # P5：可用性探测。verified=True 只证明 ACP 会话接受了这个值，
            # 不证明模型可用（15721 网关实测 /v1/models 返回 0 个）。
            # 只对 model 探测；失败不阻塞切换，只把结果带给前端。
            entry["reachable"] = None
            if verified and entry.get("category") == "model":
                reachable, err = await self._probe_model(value)
                entry["reachable"] = reachable
                if err:
                    entry["reachable_error"] = err
        return entry or {}

    # ---------- 持久化 ----------

    def load_prefs(self) -> dict[str, dict]:
        """合并两层预置：config.json 静态基线 + state.json 运行时覆盖（覆盖优先）。"""
        prefs: dict[str, dict] = {}
        try:
            cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
            for a in cfg.get("agents") or []:
                p: dict[str, Any] = {}
                if a.get("model"):
                    p["model"] = a["model"]
                if isinstance(a.get("options"), dict):
                    p.update(a["options"])
                if p:
                    prefs[a.get("id") or ""] = p
        except Exception as exc:  # noqa: BLE001
            log(f"读 config.json 预置失败: {exc}")

        try:
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            st = {}
        except Exception as exc:  # noqa: BLE001
            log(f"读 state.json 失败: {exc}")
            st = {}
        for aid, p in (st or {}).items():
            if not isinstance(p, dict):
                continue
            merged = dict(prefs.get(aid) or {})
            if p.get("model"):
                merged["model"] = p["model"]
            if isinstance(p.get("options"), dict):
                merged.update(p["options"])
            prefs[aid] = merged
        return prefs

    def save_pref(self, agent_id: str, config_id: str, value: str) -> None:
        """把一次成功切换写进 state.json（原子写：先 .tmp 再 replace）。"""
        try:
            try:
                st = json.loads(self.state_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                st = {}
            a = st.setdefault(agent_id, {})
            if config_id == "model":
                a["model"] = value
            else:
                a.setdefault("options", {})[config_id] = value
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.state_file)
        except Exception as exc:  # noqa: BLE001
            log(f"state.json 写入失败: {exc}")

    def reset_pref(self, agent_id: str, config_id: str) -> None:
        """P5：删掉 state.json 里该 agent 的这一项。

        config_id == "model" 删顶层 model 键，否则删 options.<config_id>。
        条目删空后整个 agent 条目也删掉。配合 /api/reconnect 就能回到 agent
        自己的默认值——给"用户彻底点错了"留的逃生口。不碰 config.json 静态基线。
        没有任何可删的内容时是 no-op（不写文件）。
        """
        try:
            try:
                st = json.loads(self.state_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return
            a = st.get(agent_id)
            if not isinstance(a, dict):
                return
            removed = False
            if config_id == "model" and "model" in a:
                del a["model"]
                removed = True
            elif config_id != "model" and isinstance(a.get("options"), dict):
                if config_id in a["options"]:
                    del a["options"][config_id]
                    removed = True
                if not a["options"]:
                    del a["options"]
            if not a:
                del st[agent_id]
                removed = True
            if not removed:
                return
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.state_file)
            log(f"state.json 重置 {agent_id}.{config_id}")
        except Exception as exc:  # noqa: BLE001
            log(f"state.json 重置失败: {exc}")

    # ---------- 模型可用性探测（P5，抄 OpenBot 的 model-availability-check） ----------

    def _probe_config(self) -> dict:
        """探测配置：内置默认（baseUrl 认环境变量 ACP_ROOM_PROBE_BASE，不硬编码
        端口）← config.json 的 "probe" 块覆盖。铁律只许改本文件，config.json
        我们不动——"probe" 块由 pi 决定何时加，他加了我们就会读。
        """
        cfg = dict(PROBE_DEFAULT)
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
                except (TypeError, ValueError):
                    t = 0.0
                if t > 0:
                    cfg["timeout"] = t
        except Exception as exc:  # noqa: BLE001
            log(f"读 probe 配置失败，用内置默认: {exc}")
        return cfg

    async def _probe_model(self, value: str) -> tuple[bool | None, str | None]:
        """一条 max_tokens=8 的轻量请求，验证模型在探测网关是否真的可用。

        教训（P5 实测）：verified=True 只证明 ACP 会话接受了这个值，不证明模型
        可用——15721 网关 /v1/models 返回 0 个模型时，切到 gpt-6-luna 照样
        verified=True，真实请求却是 upstream 404 模型不存在。
        探测失败/超时都不阻塞切换（用户可能就是想试新模型），只做提示。
        返回 (reachable, error)；probe 关闭 → (None, None)。
        """
        cfg = self._probe_config()
        if not cfg.get("enabled"):
            return None, None
        base = str(cfg.get("baseUrl") or "").strip().rstrip("/")
        if not base:
            # 空 baseUrl = 没探测（裁定 0010：跳过，不算失败）
            return None, None
        url = f"{base}/chat/completions"
        try:
            import httpx  # 项目 .venv 自带（FastAPI 依赖链）

            async with httpx.AsyncClient(
                    timeout=float(cfg.get("timeout") or 3)) as client:
                r = await client.post(url, json={
                    "model": value,
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
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def _apply_prefs(self, agent: "AcpAgent") -> None:
        """启动时应用持久化预置：当前值 != 预置值才切（幂等，不重复打扰 agent）。"""
        prefs = self.load_prefs().get(agent.cfg.id) or {}
        if not prefs:
            return
        for config_id, value in prefs.items():
            cur = (self.for_agent(agent.cfg.id).find(config_id) or {}).get("currentValue")
            if cur == value:
                continue
            try:
                await self.set_option(agent, config_id, value)
            except Exception as exc:  # noqa: BLE001
                log(f"[{agent.cfg.id}] 应用预置 {config_id}={value} 失败: {exc}")

    def snapshot(self) -> dict:
        return {aid: o.snapshot() for aid, o in self.by_agent.items()}

    def brief(self) -> dict:
        """只要“当前值”，不带备选列表。给每轮广播用。

        snapshot() 会把 982 个模型名全带上（实测 128KB），
        而 push_state 一轮要推好几次。前端要完整列表时去 GET /api/options。
        """
        out: dict[str, dict] = {}
        for aid, o in self.by_agent.items():
            model = o.model_entry
            out[aid] = {
                "model": o.model,
                "modelCount": len(model["options"]) if model else 0,
                "current": {it["id"]: it.get("currentValue") for it in o.items},
            }
        return out


def _main() -> None:
    import argparse
    import asyncio
    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from core.agent import AcpAgent, AgentConfig, text_of_update  # noqa: PLC0415

    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    collectors: dict[str, list[str]] = {}

    async def on_update(evt) -> None:  # noqa: ANN001
        u = (evt.get("params") or {}).get("update") or {}
        if u.get("sessionUpdate") == "agent_message_chunk":
            collectors.setdefault(evt["agent"], []).append(text_of_update(u))

    async def allow_all(evt):  # noqa: ANN001
        opts = (evt.get("params") or {}).get("options") or []
        if not opts:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": opts[0]["optionId"]}}

    async def run() -> None:
        ap = argparse.ArgumentParser(description="模型/配置项发现与切换")
        ap.add_argument("--discover", action="store_true")
        ap.add_argument("--set", nargs=3, metavar=("AGENT", "CONFIG_ID", "VALUE"))
        ap.add_argument("--verify", action="store_true", help="切换后跑一轮确认")
        ap.add_argument("--json", action="store_true", help="最后打印机器可读快照")
        ap.add_argument("--agent-only", action="store_true",
                        help="--set 时只连目标 agent（快，其余 agent 不启动）")
        args = ap.parse_args()

        store = ConfigStore(root)
        specs = [s for s in cfg["agents"] if s.get("enabled", True)]
        if args.set and args.agent_only:
            specs = [s for s in specs if s["id"] == args.set[0]]
            if not specs:
                print(f"config.json 里没有 agent {args.set[0]!r}")
                return

        agents: dict[str, AcpAgent] = {}
        for spec in specs:
            aid = spec["id"]
            try:
                c = AgentConfig(id=aid, name=spec.get("name", aid),
                                cmd=list(spec["cmd"]),
                                cwd=spec.get("cwd") or cfg["cwd"])
                a = AcpAgent(c, on_update, allow_all)
                await a.start()
                res = await a.request("session/new", {"cwd": c.cwd, "mcpServers": []})
                store.for_agent(aid).update(res)
                agents[aid] = a
            except Exception as exc:  # noqa: BLE001
                print(f"{aid:8} 连接失败: {type(exc).__name__}: {exc}")
                continue

            if args.discover:
                snap = store.for_agent(aid).snapshot()
                others = [f"{o['id']}({len(o['options'])})"
                          for o in snap["options"] if o["category"] != "model"]
                print(f"{aid:8} model={snap['model']} 可选={snap['modelCount']}  "
                      f"其它项={others}")

            if args.set and args.set[0] == aid:
                t0 = time.time()
                try:
                    entry = await store.set_option(a, args.set[1], args.set[2])
                    ms = int((time.time() - t0) * 1000)
                    print(f"{aid:8} {args.set[1]} -> {args.set[2]}  "
                          f"实际={entry.get('currentValue')}  "
                          f"verified={entry.get('verified')}  {ms}ms")
                except Exception as exc:  # noqa: BLE001
                    print(f"{aid:8} 切换失败: {type(exc).__name__}: {exc}")
                    continue

                if args.verify:
                    collectors[aid] = []
                    t0 = time.time()
                    try:
                        res = await a.prompt("只回两个字：ok")
                    except Exception as exc:  # noqa: BLE001
                        print(f"{aid:8} 验证回合失败: {type(exc).__name__}: {exc}")
                        continue
                    ms = int((time.time() - t0) * 1000)
                    usage = res.get("usage") or {}
                    text = "".join(collectors.get(aid, [])).strip() or str(
                        res.get("text") or "")
                    print(f"{aid:8} 验证回合 tokens={usage.get('totalTokens')}  "
                          f"{ms}ms stop={res.get('stopReason')} 回复={text[:80]!r}")

        if args.json:
            print(json.dumps(store.snapshot(), ensure_ascii=False))

        for a in agents.values():
            await a.stop()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
