"""G2 测试用的假 ACP agent：一个 stdio JSON-RPC 服务，能力可开关。

为什么要有它：session/resume、session/load 的分支（支持哪个、都不支持、
恢复失败要退回新开）只有拿真协议对话才测得出来。拿真 agent 测既慢又烧钱，
而且它们不一定支持。

行为由环境变量控制，方便一个文件跑出四种组合：
    STUB_CAPS        resume / resume-other-id / load / none / bad-resume
    STUB_KNOWN_SID   它"记得"的会话 id（不匹配就当恢复失败）
"""

import json
import os
import sys

caps_kind = os.environ.get("STUB_CAPS", "resume")
known_sid = os.environ.get("STUB_KNOWN_SID", "")

replay = []      # load 时回放的 update 条数
loaded = []      # 收到过的 load/resume 请求参数


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(rid, res):
    send({"jsonrpc": "2.0", "id": rid, "result": res})


def error(rid, code, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def capabilities():
    ac = {"loadSession": False}
    if caps_kind == "resume":
        ac["sessionCapabilities"] = {"resume": {}}
    elif caps_kind == "resume-other-id":
        # 声明支持，但恢复时回一个**新的** sessionId（claude / codex 实测就这样）
        ac["sessionCapabilities"] = {"resume": {}}
    elif caps_kind == "load":
        ac["loadSession"] = True
    elif caps_kind == "bad-resume":
        # 声明支持，但真调用时报错 —— 恢复失败必须能退回新开
        ac["sessionCapabilities"] = {"resume": {}}
    return {"protocolVersion": 1, "agentCapabilities": ac,
            "agentInfo": {"name": "stub", "version": "0"}}


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            result(rid, capabilities())
        elif method == "session/new":
            result(rid, {"sessionId": "S-NEW", "models": None})
        elif method in ("session/load", "session/resume"):
            loaded.append(params)
            if caps_kind == "bad-resume" or (known_sid and params.get("sessionId") != known_sid):
                error(rid, -32603, "no such session")
                continue
            if method == "session/load":
                # 真 agent 会把整段历史当通知回放。客户端必须吞掉，
                # 否则界面上会长出幽灵发言。
                for i in range(3):
                    send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": params.get("sessionId"),
                        "update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"type": "text", "text": f"replay{i}"}}}})
                replay.append(params.get("sessionId"))
            result(rid, {"sessionId": "S-FRESH"} if caps_kind == "resume-other-id" else {})
        elif method == "session/prompt":
            result(rid, {"stopReason": "end_turn"})
        elif rid is not None:
            error(rid, -32601, f"stub 不实现 {method}")


if __name__ == "__main__":
    main()
