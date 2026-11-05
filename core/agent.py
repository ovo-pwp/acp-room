"""ACP (Agent Client Protocol) 客户端 —— 把一个命令行 agent 变成平台里的一个成员。

它做的事只有四件：
1. spawn 一个 ACP server 子进程（stdio JSON-RPC 2.0）
2. initialize → session/new 建立会话
3. session/prompt 发一轮话，同时把 session/update 的流式事件转成回调
4. 应答 agent 反向发来的请求（申请权限等），否则 agent 会卡死

协议文档 / schema: https://agentclientprotocol.com  （vendor/../schema/v1/schema.json）

刻意的取舍：
- **不提供 fs / terminal 能力**（clientCapabilities 里不声明）。
  agent 自己有读写文件和跑命令的工具，让它用自己的，平台不去替它做文件 IO。
  平台只做"传话 + 显示"，这跟 A2A "preserve opacity" 的精神一致。
- **权限请求可以转给人类**，也可以按配置自动放行。默认转给人类。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.gui_log import log


def resolve_cmd(cmd: list[str]) -> list[str]:
    """Windows 上 npx/npm/omp 是 .cmd 包装，CreateProcess 必须有扩展名。"""
    exe = cmd[0]
    found = shutil.which(exe)
    if not found:
        for ext in (".cmd", ".exe", ".bat"):
            found = shutil.which(exe + ext)
            if found:
                break
    if not found:
        raise FileNotFoundError(f"找不到可执行文件: {exe}（PATH 里没有）")
    return [found, *cmd[1:]]


@dataclass
class AgentConfig:
    id: str
    name: str
    cmd: list[str]
    cwd: str
    color: str = "#888888"
    permission: str = "ask"          # ask | allow | deny
    model_hint: str = ""             # 仅用于界面显示
    env: dict[str, str] = field(default_factory=dict)
    # 人格。三个字段都进 prompt 的身份头 —— 见 core/persona.py。
    # 没有它们时，四个 agent 只能说"通用助手"话，圆桌会变成四个复读机。
    role: str = ""                   # 完整角色描述（第二人称，写给 agent 自己看）
    tag: str = ""                    # 一行短标签（写给别人看的名册里）
    style: str = ""                  # 说话方式


class AcpError(RuntimeError):
    pass


class AcpAgent:
    """一个 ACP agent 的连接。可反复 prompt，进程死了会自动重启。"""

    def __init__(
        self,
        cfg: AgentConfig,
        on_update: Callable[[dict], Awaitable[None]],
        on_permission: Callable[[dict], Awaitable[dict]],
    ) -> None:
        self.cfg = cfg
        self.on_update = on_update
        self.on_permission = on_permission
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self.session_info: dict[str, Any] = {}
        self.info: dict[str, Any] = {}
        # G2：上次会话的 id。由 room 在 start() 之前塞进来；
        # 支持的话就 resume/load 回去，agent 侧就还记得之前聊过什么。
        self.resume_session_id: str | None = None
        self.session_origin = "new"       # new | resume | load
        # 接回上次会话失败的原因原文（成功时是空串）。界面上要能说出
        # "为什么没接回"，光一个 session_origin=new 分不出"没什么可接"和"接挂了"。
        self.resume_error = ""
        self._loading = False             # True 时吞掉 session/update 回放
        self.replayed_updates = 0
        self._nid = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.stderr_tail: list[str] = []
        self.alive = False
        self.busy = False
        self.lock = asyncio.Lock()   # 同一 agent 同时只能跑一轮

    # ---------- 进程与协议 ----------

    async def start(self) -> dict:
        if self.alive:
            return self.info
        cmd = resolve_cmd(self.cfg.cmd)
        env = {**os.environ, **self.cfg.env, "PYTHONIOENCODING": "utf-8"}
        log(f"[{self.cfg.id}] spawn: {' '.join(cmd)} (cwd={self.cfg.cwd})")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.cfg.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
            )
        except Exception as exc:  # noqa: BLE001
            raise AcpError(f"启动失败: {type(exc).__name__}: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

        init = await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                # 平台不代做文件/终端操作，agent 用自己的工具
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                "clientInfo": {"name": "acp-room", "version": "0.1.0"},
            },
            timeout=180,
        )
        self.info = init
        self.alive = True
        log(f"[{self.cfg.id}] initialize ok: "
            f"{init.get('agentInfo', {}).get('title') or init.get('agentInfo', {}).get('name')} "
            f"v{init.get('agentInfo', {}).get('version')}")
        await self.open_session()
        return init

    async def open_session(self) -> str:
        """建立会话：能接回上次的就接回，否则新开。

        优先 session/resume —— 它只恢复上下文、不把整段历史回放给客户端，
        省一次几百条通知的往返；agent 只声明 loadSession 时退到 session/load
        （回放期间 _loading 会吞掉那些 update，界面上不会看到"幽灵发言"）。
        两个都不支持、或调用失败，就 fall back 到 session/new —— 恢复失败
        绝不能把 agent 变成不可用。
        """
        target = self.resume_session_id
        if target:
            caps = (self.info.get("agentCapabilities") or {})
            sc = (caps.get("sessionCapabilities") or {})
            # resume 的能力标记是一个空对象 {}，不是 true —— bool({}) 是 False，
            # 所以这里必须判"键在不在"。实测踩过：判真值导致永远走不到 resume。
            can_resume = "resume" in sc
            can_load = bool(caps.get("loadSession"))
            method = "session/resume" if can_resume else ("session/load" if can_load else None)
            if method:
                self._loading = True
                self.replayed_updates = 0
                try:
                    res = await self.request(method, {
                        "sessionId": target,
                        "cwd": os.path.abspath(self.cfg.cwd),
                        "mcpServers": [],
                    }, timeout=180)
                    returned = (res or {}).get("sessionId") or ""
                    self.session_id = returned or target
                    self.session_info = res or {}
                    # 实测：resume 成功时 codex / claude **根本不回 sessionId**
                    # （响应只有 models/modes/configOptions），所以取请求的那个才对。
                    # 万一适配器回了个不一样的 id，那是它自己起了新会话 —— 必须标
                    # "new"，否则界面上那句「接回上次会话」是假的，
                    # 而用户会据此以为它还记得前文。
                    if returned and returned != target:
                        self.session_origin = "new"
                        self.resume_error = f"{method} 回了另一个会话 id"
                        log(f"[{self.cfg.id}] {method} 给了新会话"
                            f"（{target[:8]} → {returned[:8]}）：没接回，按新会话记")
                    else:
                        self.session_origin = ("resume" if method == "session/resume"
                                               else "load")
                        log(f"[{self.cfg.id}] {method} ok session={self.session_id} "
                            f"回放吞掉 {self.replayed_updates} 条 update")
                    return self.session_id
                except Exception as exc:  # noqa: BLE001
                    # 原话要留下来。"no rollout found for thread id X" 和
                    # "not logged in" 是完全不同的两件事：前者说明那次会话压根
                    # 没落盘（没什么可接回），后者说明线程还在、只是这次起不来，
                    # 下次该再试同一个 id。只看 session_origin=new 分不出来。
                    self.resume_error = str(exc)[:300]
                    log(f"[{self.cfg.id}] 恢复 {method} 失败（{exc}）→ 新开一个 session")
                finally:
                    self._loading = False
            else:
                log(f"[{self.cfg.id}] 不支持 resume/load（caps={caps}）→ 新开 session")
        return await self.new_session()

    async def new_session(self) -> str:
        res = await self.request(
            "session/new",
            {"cwd": os.path.abspath(self.cfg.cwd), "mcpServers": []},
            timeout=180,
        )
        sid = res.get("sessionId")
        if not sid:
            raise AcpError(f"session/new 没有返回 sessionId: {res}")
        self.session_id = sid
        self.session_origin = "new"
        # session/new 的返回值里带 configOptions（模型列表等），存起来给 config_opts 用
        self.session_info = res
        log(f"[{self.cfg.id}] session={sid} models={json.dumps(res.get('models'), ensure_ascii=False)[:160]}")
        return sid

    async def stop(self) -> None:
        self.alive = False
        for t in (self._reader_task, self._stderr_task):
            if t:
                t.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            self.stderr_tail.append(text[:300])
            del self.stderr_tail[:-40]
            log(f"[{self.cfg.id} stderr] {text[:200]}")

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            raw = line.decode("utf-8", "replace").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log(f"[{self.cfg.id} 非 JSON] {raw[:200]}")
                continue
            await self._dispatch(msg)
        # 进程退出：把挂起的请求全部失败掉，避免上层永久等待
        self.alive = False
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError(f"{self.cfg.id} 进程已退出"))
        self._pending.clear()
        log(f"[{self.cfg.id}] 进程结束 rc={self.proc.returncode if self.proc else '?'}")

    async def _dispatch(self, msg: dict) -> None:
        if "id" in msg and "method" in msg:
            # agent → client 的反向请求
            asyncio.create_task(self._handle_server_request(msg))
            return
        if "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(AcpError(json.dumps(msg["error"], ensure_ascii=False)))
                else:
                    fut.set_result(msg.get("result") or {})
            return
        method = msg.get("method")
        if method == "session/update":
            if self._loading:
                # session/load 会把整段历史当通知回放一遍。这些不是"当前发言"，
                # 广播出去界面就会长出幽灵气泡。只计数，不转发。
                self.replayed_updates += 1
                return
            await self.on_update({"agent": self.cfg.id, "params": msg.get("params") or {}})
        else:
            log(f"[{self.cfg.id} 通知] {method}")

    async def _handle_server_request(self, msg: dict) -> None:
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            # 宿主侧闸门只判一次：这里拿到 result 后原样回给 agent，不再二次询问。
            # 三个 outcome 必须都妥善返回——agent 收不到 result 会一直挂着本 turn。
            try:
                result = await self.on_permission({"agent": self.cfg.id, "params": params})
            except Exception as exc:  # noqa: BLE001
                result = {"outcome": {"outcome": "cancelled"}}
                log(f"[{self.cfg.id} 权限处理异常] {exc}")
            if not isinstance(result, dict) or "outcome" not in result:
                log(f"[{self.cfg.id} 权限处理返回形状不对：{result!r}，按 cancelled 回")
                result = {"outcome": {"outcome": "cancelled"}}
            await self._write({"jsonrpc": "2.0", "id": rid, "result": result})
            return
        # 平台不提供 fs / terminal，明确回 -32601，agent 会退回自家工具
        await self._write({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32601, "message": f"acp-room 不提供 {method}"},
        })
        log(f"[{self.cfg.id}] 拒绝了 client 请求 {method}")

    async def _write(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise AcpError("进程不在")
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 600) -> dict:
        self._nid += 1
        rid = self._nid
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    # ---------- 对话 ----------

    async def prompt(self, text: str, timeout: float = 900,
                     images: list[dict] | None = None) -> dict:
        """跑一轮，返回 {stopReason, text, usage}。流式内容通过 on_update 推出去。

        images：ACP image 内容块（{"type":"image","mimeType":..,"data":base64}）。
        顺序是「正文 + 图片」——先给任务再给图，模型更容易把图当证据而不是当主题。
        没有图片时 prompt 数组与原来完全一致，不影响任何既有调用方。
        """
        async with self.lock:
            if not self.alive:
                await self.start()
            if not self.session_id:
                # 原来是裸 `assert self.session_id`，失败时抛的是
                # `AssertionError: `（**消息为空**），界面上只显示
                # 「⚠ AssertionError:」——完全看不出发生了什么。
                # 实测就踩过：pi 适配器启动失败（Cannot call write after a
                # stream was destroyed），而人看到的是一个空断言。
                raise AcpError(
                    f"[{self.cfg.id}] 没有可用的 session（连接可能失败过）。"
                    f"先看它的启动日志——常见原因是命令不存在、未登录、"
                    f"或适配器 stdout 被非协议内容污染。"
                )
            self.busy = True
            try:
                content: list[dict] = [{"type": "text", "text": text}]
                content.extend(images or [])
                res = await self.request(
                    "session/prompt",
                    {
                        "sessionId": self.session_id,
                        "prompt": content,
                    },
                    timeout=timeout,
                )
                return res
            finally:
                self.busy = False

    async def cancel(self) -> None:
        if not self.session_id:
            return
        try:
            await self._write({
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": self.session_id},
            })
        except Exception as exc:  # noqa: BLE001
            log(f"[{self.cfg.id} cancel 失败] {exc}")


def text_of_update(update: dict) -> str:
    """从 session/update 里取出可直接显示的文本（如果有）。"""
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        c = update.get("content") or {}
        if c.get("type") == "text":
            return c.get("text") or ""
    return ""
