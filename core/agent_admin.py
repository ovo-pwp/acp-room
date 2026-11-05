"""运行期增删 agent —— 不用改 config.json 再重启。

## 为什么放在独立模块

`Room.__init__` 里构建 `specs` / `agents` 那一段只跑一次（启动时）。要让"页面上点一下就
生效"，需要把同一段逻辑抽成可重入的方法，并且**先探活再落地**：

    校验输入 -> 起进程探活（initialize + session/new）-> 写 config.json -> 注册进 Room

顺序不能反。先写 config 再探活，一个写错的命令会让下次重启整个房间起不来，
而人只会在日志里看到一行 Traceback。

## 探活为什么单独起进程

不能拿 Room 里正在用的实例试——`initialize` 重复调用、`session/new` 再造一个 session，
都会污染它正在聊的上下文。所以探活用一次性的临时进程，跑完就关。
代价是慢 3~8 秒（npx 拉包更久），换来的是"点添加"不会弄坏正在进行的对话。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from core.gui_log import log

# agent id 会进路由、审计、config.json，只允许安全字符
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")

# 常见 ACP agent 的一键预设。命令抄自 vendor/acpx/src/agent-registry.ts，
# 但这里只列**本机能直接跑**和**最常见的几个**，避免给出一个装不上的清单。
PRESETS = [
    {"id": "kimi",    "name": "Kimi Code",   "cmd": ["kimi", "acp"]},
    {"id": "qwen",    "name": "Qwen Code",   "cmd": ["qwen", "--acp"]},
    {"id": "gemini",  "name": "Gemini CLI",  "cmd": ["gemini", "--acp"]},
    {"id": "cursor",  "name": "Cursor",      "cmd": ["cursor-agent", "acp"]},
    {"id": "copilot", "name": "GitHub Copilot", "cmd": ["copilot", "--acp", "--stdio"]},
    {"id": "goose",   "name": "Goose",       "cmd": ["goose", "acp"]},
    {"id": "opencode","name": "OpenCode",    "cmd": ["npx", "-y", "--package=opencode-ai", "--", "opencode", "acp"]},
    {"id": "droid",   "name": "Factory Droid", "cmd": ["droid", "exec", "--output-format", "acp"]},
    {"id": "dsh",     "name": "DeepSeek Harness", "cmd": ["dsh", "--profile", "acp"]},
]


class AgentError(ValueError):
    """能直接显示给人看的失败原因。"""


def validate_spec(body: dict) -> dict:
    """把前端传来的东西洗成一个合法的 agent 配置。"""
    aid = str(body.get("id") or "").strip().lower()
    if not _ID_RE.match(aid):
        raise AgentError("id 只能是小写字母/数字/下划线/连字符，2~32 位，如 kimi")
    cmd = body.get("cmd")
    if isinstance(cmd, str):
        # 允许人直接粘一行命令。用 shlex 切，但 Windows 下反斜杠路径会被吃掉，
        # 所以只做最保守的切分：按空白切，引号内的空白保留。
        cmd = _split_cmd(cmd)
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise AgentError("cmd 必须是非空字符串数组，如 [\"kimi\", \"acp\"]")
    cmd = [c for c in cmd if c.strip()]
    if not cmd:
        raise AgentError("cmd 不能为空")

    perm = str(body.get("permission") or "allow")
    if perm not in ("allow", "deny", "ask"):
        raise AgentError("permission 只能是 allow / deny / ask")

    raw_color = str(body.get("color") or "").strip()
    color = raw_color if re.fullmatch(r"#[0-9a-fA-F]{6}", raw_color) else ""

    out = {
        "id": aid,
        "name": str(body.get("name") or aid)[:40],
        "cmd": cmd,
        "model_hint": str(body.get("model_hint") or "")[:80],
        "permission": perm,
        "enabled": True,
    }
    # color 和人格三个字段一样：**只在真的给了合法值时才写**。
    # 否则重加一个已有 agent 时，一个没动过的默认色会把它的身份色覆盖掉
    # （upsert 是 {**旧, **新} 合并，写死一个默认值就等于强制改色）。
    # 新 agent 没有 color 键也没关系：_spec_from_cfg 会回落 #888888。
    if color:
        out["color"] = color
    # 人格三个字段同理。除了会被覆盖，还有一条：写了空串会盖掉
    # core/persona.py 里按 id 匹配的内置人格，新人反而拿到更差的通用腔。
    role = str(body.get("role") or "").strip()
    tag = str(body.get("tag") or "").strip()
    style = str(body.get("style") or "").strip()
    if role:
        out["role"] = role[:400]
    if tag:
        out["tag"] = tag[:80]
    if style:
        out["style"] = style[:120]
    return out


def _split_cmd(line: str) -> list[str]:
    out, buf, quote = [], "", ""
    for ch in line.strip():
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf += ch
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


async def probe(cmd: list[str], cwd: str, timeout: float = 25.0) -> dict:
    """起一个一次性 ACP 进程，跑 initialize + session/new，然后关掉。

    返回 {ok, agentInfo, models, sessionId, error, elapsed_ms}
    任何一步失败都返回 ok=False 和一句人话原因，不抛异常——调用方要展示它。
    """
    from core.agent import AcpAgent, AgentConfig

    t0 = time.time()
    spec = AgentConfig(id="__probe__", name="probe", cmd=list(cmd), cwd=cwd,
                       color="#888888", permission="allow", model_hint="", env={})
    agent = AcpAgent(spec, _noop_update, _refuse_permission)
    try:
        info = await asyncio.wait_for(agent.start(), timeout=timeout)
        return {
            "ok": True,
            "agentInfo": (info or {}).get("agentInfo") or {},
            "models": bool((agent.session_info or {}).get("models")),
            "sessionId": agent.session_id,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"探活超时（{int(timeout)}s）——命令可能在等输入，或 npx 在拉包",
                "elapsed_ms": int((time.time() - t0) * 1000)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        hint = ""
        low = msg.lower()
        if "enoent" in low or "不是内部或外部命令" in msg or "cannot find" in low or "找不到" in msg:
            hint = "命令不存在：先确认它已安装并在 PATH 里"
        elif "unauth" in low or "login" in low or "401" in msg or "授权" in msg:
            hint = "认证没过：先在终端里单独登录一次"
        return {"ok": False, "error": (hint + "｜" if hint else "") + msg,
                "elapsed_ms": int((time.time() - t0) * 1000)}
    finally:
        try:
            await agent.stop()
        except Exception:  # noqa: BLE001
            pass


async def _noop_update(evt: dict) -> None:  # pragma: no cover
    return None


async def _refuse_permission(evt: dict) -> dict:  # pragma: no cover
    # 探活期间不授权任何东西：这里只是确认"能连通、能建 session"。
    return {"outcome": {"outcome": "cancelled"}}


# ---------------------------------------------------------------- 持久化


CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"agents": []}
    except (OSError, ValueError) as exc:
        # 读失败要说出来：静默返回空配置会让下一次"添加 agent"把整个文件覆盖成只有一条。
        raise AgentError(f"读 {CONFIG} 失败（{type(exc).__name__}: {exc}），为安全起见不修改") from exc


def write_config(cfg: dict) -> None:
    """无 BOM 写回。带 BOM 会让 json.loads(encoding="utf-8") 抛错——踩过一次。"""
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_bytes(json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"))
    os.replace(tmp, CONFIG)


def upsert_agent(spec: dict) -> str:
    """写进 config.json 的 agents[]。返回 'added' 或 'updated'。"""
    cfg = read_config()
    agents = cfg.setdefault("agents", [])
    for i, a in enumerate(agents):
        if a.get("id") == spec["id"]:
            agents[i] = {**a, **spec}
            write_config(cfg)
            return "updated"
    agents.append(spec)
    write_config(cfg)
    return "added"


def remove_agent(aid: str) -> bool:
    cfg = read_config()
    agents = cfg.get("agents") or []
    kept = [a for a in agents if a.get("id") != aid]
    if len(kept) == len(agents):
        return False
    cfg["agents"] = kept
    write_config(cfg)
    return True
