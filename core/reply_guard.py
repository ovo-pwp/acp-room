"""回复守卫：判断一条模型输出是不是"真的在说话"。

## 为什么需要

网关错误、traceback、tool-call JSON 不是发言，但过去会被原样写进转录，
长得和正常回答一样。实测：

    omp  402 Insufficient Balance (request_id: ...) (type=insufficient_balance_error)
    omp  401 Authentication Fails, Your api key: ****6ad2 is invalid ...
    codex ⚠ ACP 错误：{"code": -32000, "message": "Authentication required"}

这三条全被记成 `ok`。更糟的是**它们污染了别人的上下文**——别的 agent
读到之后当成内容来评论（转录里真有「OMP 那几次 401/402 没答上」这种句子）。
错误一旦进了转录，就变成了会被反复引用的"发言"。

## 设计原则：宁可漏拦，不可误杀

一个真回答里提到 401、贴一段 traceback、讨论密钥泄漏，都是**正常**的。
所以这里只收"整条输出就是一个故障"这种强信号，不做关键词扫描。
误杀的代价是**静默丢掉一个真回答**，比显示一条错误糟得多。

每条规则都要求信号出现在**靠近开头**的位置，或者**整条文本**才匹配。
拦下来的都记审计（`turn_blocked`），漏拦了还能回头查。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# 判断"开头附近"：真回答即使引用错误，前面也会有铺垫
_HEAD_CHARS = 160

# ---- 网关 / 适配器错误 ----
# 这里是各家真实吐出来的形状，不是猜的：
#   DeepSeek: 401 Authentication Fails, Your api key: ****6ad2 is invalid (request_id: ...) (type=authentication_error ...)
#   DeepSeek: 402 Insufficient Balance (request_id: ...) (type=insufficient_balance_error ...)
#   Agent2API: {"error":{"code":"model_not_found","message":"模型不存在: ..."}}
#   房间自己: ⚠ ACP 错误：{"code": -32000, ...}
_GATEWAY = re.compile(
    # request_id 必须是 UUID 形状。放宽成"任意串"会误杀——一段正常回答里
    # 引用了一句 `(request_id: abc-123)` 就会被当成错误（实测踩过）。
    r"\(\s*request_id\s*:\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*\)"
    r"|\(\s*type\s*=\s*\w+_error\b"
    r"|Your api key:\s*\*+[0-9a-zA-Z]+ is invalid"
    r"|\"code\"\s*:\s*-?\d{3,5}\s*,\s*\"message\""
    r"|\b(invalid_api_key|model_not_found|insufficient_balance_error|authentication_error)\b",
    re.I,
)

# 房间自己的包装前缀
_ACP_ERR_PREFIX = re.compile(r"^\s*⚠\s*ACP\s*错误\s*[:：]")

# ---- 崩溃栈 ----
_TRACEBACK = re.compile(r"^\s*Traceback \(most recent call last\)\s*:", re.M)

# ---- 元叙述：模型在描述"我做了什么"，而不是做 ----
_META_WHOLE = re.compile(
    r"^\s*(已(发送|发布|回复|提交|记录)|消息已(发送|发布)|回复已(发布|发送)"
    r"|Reply (posted|sent) successfully|Message (posted|sent)"
    r"|Done[.。]?)\s*[。.!！]?\s*$",
    re.I,
)

# ---- 拒答套话：必须**整条**都是套话才算 ----
_REFUSAL_WHOLE = re.compile(
    r"^\s*(I'?m sorry,? but I (can'?t|cannot)|I (can'?t|cannot) (help|assist)"
    r"|As an AI( language model)?,? I (can'?t|cannot)"
    r"|抱歉[，,]?\s*(我|作为)|很抱歉[，,]?\s*我无法"
    r"|我无法(协助|帮助|回答)(你)?(这个|该)?问题)",
    re.I,
)

# ---- 密钥泄漏 ----
_LEAK = re.compile(
    r"\bsk-[A-Za-z0-9_\-]{20,}"
    r"|\bBearer\s+[A-Za-z0-9._\-]{24,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

# ---- tool-call 原文倾倒 ----
_JSON_KEYS = {"tool_calls", "tool_call", "function", "arguments", "tool_use", "tool_name"}


@dataclass(frozen=True)
class Verdict:
    """一条回复的判定结果。`blocked=False` 时其余字段无意义。"""
    blocked: bool
    kind: str = ""       # gateway_error / traceback / tool_json / refusal / meta / leak / repetition
    reason: str = ""     # 人话，写进审计
    hint: str = ""       # 给用户看的下一步建议

    def __bool__(self) -> bool:      # 方便 `if guard.check(t):`
        return self.blocked


OK = Verdict(False)


def redact(text: str, limit: int = 120) -> str:
    """把被拦下的正文做脱敏截断，供审计留痕。

    为什么必须留原文：被拦下的轮次不进转录，所以**误杀是不可见的**——
    没有原文就永远无法复核"这条到底该不该拦"。房间里的 claude 提过这一点：

        > 这种守卫误杀不留痕……误杀率永远无法测量，也无法通过调参来回滚。

    但原文里可能真的带密钥（`leak` 那一类就是这么判出来的），
    直接写审计等于二次泄漏，所以先把强特征打码再截断。
    """
    s = text or ""
    s = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "sk-***", s)
    s = re.sub(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***", s)
    s = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----", s)
    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _head(text: str) -> str:
    return text.lstrip()[:_HEAD_CHARS]


def _error_shaped(t: str) -> bool:
    """错误正文的"体型"：短、行数极少。

    真回答即使引用了一条错误，通常也有铺垫和后续（多行、有段落）。
    网关错误则是**整条就那一句**。这条形状约束是防误杀的第二道闸：
    光靠关键字，一段正常的分析文字会被吃掉。
    """
    if len(t) > 400:
        return False
    return sum(1 for ln in t.splitlines() if ln.strip()) <= 2


def _looks_like_error_json(text: str) -> bool:
    """网关有时把错误包成 JSON 正文返回。

    实测 Agent2API：{"error":{"code":"model_not_found","message":"模型不存在: ..."}}
    这种 `code` 是字符串不是数字，光靠正则抓不到，得先解析。
    """
    s = text.strip()
    if not s.startswith("{") or len(s) > 2000:
        return False
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    if isinstance(err, dict):
        return bool(err.get("message") or err.get("code") or err.get("type"))
    # 有些适配器直接平铺
    return bool(data.get("error") and ("message" in data or "code" in data))


def _looks_like_tool_json(text: str) -> bool:
    s = text.strip()
    if not s or s[0] not in "{[":
        return False
    if len(s) > 4000:            # 太长的多半是正常内容里带了 JSON
        return False
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return False

    def keys_of(o) -> set[str]:
        if isinstance(o, dict):
            k = set(o.keys())
            for v in o.values():
                k |= keys_of(v)
            return k
        if isinstance(o, list):
            k: set[str] = set()
            for v in o:
                k |= keys_of(v)
            return k
        return set()

    return bool(keys_of(data) & _JSON_KEYS)


def _runaway(text: str) -> bool:
    """同一句/同一段反复输出——模型卡住了，不是发言。"""
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 10]
    if len(lines) >= 8:
        head = lines[0]
        same = sum(1 for ln in lines if ln == head)
        if same >= 6 and same / len(lines) > 0.6:
            return True
    # 同一段 40 字以上的片段出现 5 次以上
    if len(text) >= 200:
        for size in (60, 40):
            seen: dict[str, int] = {}
            for i in range(0, len(text) - size, size):
                seg = text[i:i + size]
                seen[seg] = seen.get(seg, 0) + 1
            if seen and max(seen.values()) >= 5:
                return True
    return False


def check(text: str, *, stop_reason: str = "") -> Verdict:
    """判断这条回复该不该写进转录。

    `stop_reason` 目前只用于诊断信息，不参与判定——
    实测 end_turn 也可能带错误正文，max_tokens 也可能正常说完。
    """
    t = (text or "").strip()
    if not t:
        return OK                            # 空正文由调用方按 empty 处理

    head = _head(t)

    # 网关错误要同时满足"有强标记"+"体型像错误"。
    # 位置检查曾经用过（标记必须在前 40 字），但那是个随手定的阈值，
    # 实测 `(type=a_error)` 落在第 41 位就被卡掉了。有体型约束之后位置检查是多余的：
    # 一段正常的分析文字有多行、有铺垫，会被 `_error_shaped` 挡在门外。
    looks_error = (
        _ACP_ERR_PREFIX.search(t) is not None
        or _GATEWAY.search(t) is not None
        or _looks_like_error_json(t)
    )
    if looks_error and _error_shaped(t):
        return Verdict(
            True, "gateway_error",
            "回复正文其实是网关/适配器的错误信息",
            "这台 agent 的模型或密钥有问题，去「设置 → 配置」看它的当前模型",
        )

    if _TRACEBACK.search(head):
        return Verdict(
            True, "traceback",
            "回复正文是一段 Python 崩溃栈",
            "adapter 进程可能挂了，先「重连全部 agent」",
        )

    if _looks_like_tool_json(t):
        return Verdict(
            True, "tool_json",
            "回复正文是 tool-call 的原始 JSON",
            "适配器没把工具调用渲染成文本，换模型或升级适配器",
        )

    if _LEAK.search(t):
        return Verdict(
            True, "leak",
            "回复里带有疑似明文密钥",
            "换掉这把 key；它已经出现在转录里了",
        )

    if len(t) <= 400 and _META_WHOLE.match(t):
        return Verdict(
            True, "meta",
            "模型只在描述自己做了什么，没有实际内容",
            "重发一次；连续出现说明 prompt 的收尾约束太弱",
        )

    if len(t) <= 400 and _REFUSAL_WHOLE.match(t):
        return Verdict(
            True, "refusal",
            "整条回复是拒答套话",
            "看看是不是人格 prompt 和任务冲突了",
        )

    if _runaway(t):
        return Verdict(
            True, "repetition",
            "回复在原地重复同一段内容",
            "多半是模型卡住了，重发或换模型",
        )

    return OK
