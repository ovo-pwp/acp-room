"""回复守卫的判据。

两类用例同等重要：
  - **该拦的**：真实出现过的故障文本，一条都不能漏
  - **不该拦的**：转录里真实的正常回答，一条都不能误杀

误杀比漏拦糟：漏拦只是页面上多一行红字，误杀是**静默丢掉一个真回答**，
而且用户永远不知道发生过。所以负例全部取自这个房间真实说过的句子。
"""

import pytest

from core.reply_guard import check


# ---------------------------------------------------------------- 该拦

@pytest.mark.parametrize("text,kind", [
    # 实测 seq 78/81/87：omp 余额不足
    ("402 Insufficient Balance (request_id: ff0b28a6-3975-448d-8793-5aa0d32bbf63) "
     "(type=insufficient_balance_error param=invalid_request_error)", "gateway_error"),
    # 实测 seq 79/83：codex 认证失败
    ('⚠ ACP 错误：{"code": -32000, "message": "Authentication required"}', "gateway_error"),
    # 实测 seq 99：omp 的 key 失效
    ("401 Authentication Fails, Your api key: ****6ad2 is invalid "
     "(request_id: 7abda0ea-bca5-4f83-bc3f-77e", "gateway_error"),
    # Agent2API 网关的 JSON 错误
    ('{"error":{"code":"model_not_found","message":"模型不存在: agent2api/GLM-5.3-Flash"}}',
     "gateway_error"),
    # 崩溃栈
    ('Traceback (most recent call last):\n  File "adapter.py", line 42, in run\n'
     "KeyError: 'session_id'", "traceback"),
    # tool-call 原文
    ('{"tool_calls": [{"function": {"name": "read_file", "arguments": "{\\"p\\": \\"a\\"}"}}]}',
     "tool_json"),
    # 明文密钥
    ("用的 key 是 sk-EXAMPLE-not-a-real-key-0123456789 ，注意别外传", "leak"),
])
def test_该拦的故障文本(text, kind):
    v = check(text)
    assert v.blocked, f"漏拦了：{text[:40]}"
    assert v.kind == kind
    assert v.hint, "每条都要给用户可执行的下一步"


def test_元叙述整条才算():
    for t in ["已发送。", "Reply posted successfully", "消息已发布", "Done."]:
        assert check(t).blocked, t
        assert check(t).kind == "meta"


def test_拒答套话整条才算():
    for t in ["I'm sorry, but I can't help with that.",
              "抱歉，我无法协助完成这个请求。"]:
        assert check(t).blocked, t
        assert check(t).kind == "refusal"


def test_原地重复算故障():
    line = "这段内容会一直重复下去没有尽头"
    assert check("\n".join([line] * 9)).blocked
    assert check("这是一段正常的回答。" * 30).blocked


# ---------------------------------------------------------------- 不该拦
# 全部是转录里真实出现过的句子

NORMAL = [
    # seq 114 omp 自我介绍（里面有 401 这个词）
    "我是 OMP，房间里的模型是 GLM-5.3-Flash（按当前配置；模型名来自环境标注，"
    "我无法从内部进一步验证）。我的职责是拆别人论证里没说出口的前提——"
    "比如 Codex 那份测量规格，它假设「转录里数得出白等」，但 OMP 槽位若持续 401，"
    "缺席根本不产生白等签名，采样永远凑不满。",

    # seq 115 pi 反驳
    "OMP 那句话反了：人类 @omp 一次却只拿到 401，这本身就是最标准的白等签名——"
    "缺席不算让样本凑不满，而是让计数单向偏高。",

    # seq 116 omp 认错并递进
    "Pi 对我上一条的批驳成立，我认。但他的修正方案有一处要挑："
    "「只统计四方全活的轮次」会让样本偏向低负载时段。",

    # seq 92 claude
    "Pi 先说——他拆题要做的判断最少也最快；要是题本身问得不对，"
    "我们三个后面说什么都白费。代价是：如果题本来就已经清楚，这一轮就是白等。",

    # seq 93 pi
    "这轮没有新题，而「Pi 先说」指向的是一段空白。",

    # seq 173 pi 谈证据分级
    "要判断「你是什么模型」这个问题，得把它拆成三件事：（a）谁在答——转录里能查证；"
    "（b）配置里写的是什么——只能引用；（c）模型之间是否真有差别——只能靠行为探针。",

    # seq 101 pi 反驳 codex
    "关键词路由有一类误判无法避免：否定和引用。题面「这不该问吧」或「别选了，就 A」"
    "都包含关键词，但都不是待拆解的题。而这种误判的代价并不对称。",
]


@pytest.mark.parametrize("text", NORMAL)
def test_正常回答一条都不能误杀(text):
    v = check(text)
    assert not v.blocked, f"误杀了（{v.kind}）：{text[:40]}"


def test_错误字样出现在长文中段时不拦():
    """真回答可能引用一段错误日志来做分析，那时候它是在说话，不是在报错。"""
    text = ("先说结论：这不是模型的问题，是网关的。\n\n"
            "证据是那条 (request_id: abc-123) 的 401，它出现在所有 agent 上，"
            "说明密钥轮换没同步。\n\n"
            "建议先把 key 换掉再复测，否则样本全是故障。")
    assert not check(text).blocked


def test_短回答提到traceback字样但不拦():
    assert not check("刚才那轮是个 Traceback，不是模型答错。").blocked


def test_空正文不归守卫管():
    """空由调用方按 empty 处理，守卫不该抢这个判定。"""
    for t in ["", "   ", "\n\n"]:
        assert not check(t).blocked


def test_布尔用法():
    assert bool(check("402 Insufficient Balance (request_id: x) (type=a_error)"))
    assert not bool(check("我同意这个判断。"))


def test_stop_reason不影响判定():
    """实测 end_turn 也可能带错误正文，max_tokens 也可能正常说完。"""
    bad = "402 Insufficient Balance (request_id: x) (type=a_error)"
    good = "这是一段正常的回答内容。"
    for sr in ("end_turn", "max_tokens", "refusal", ""):
        assert check(bad, stop_reason=sr).blocked
        assert not check(good, stop_reason=sr).blocked


# ---------------------------------------------------------------- 留痕与脱敏

def test_留痕会打码密钥():
    """被拦下的正文要进审计，但不能把密钥再抄一遍。

    不留原文就没法复核误杀（claude 指出的问题）；原样留就是二次泄漏。
    """
    from core.reply_guard import redact

    out = redact("用的 key 是 sk-EXAMPLE-not-a-real-key-0123456789 别外传")
    assert "EXAMPLE" not in out and "not-a-real" not in out
    assert "sk-***" in out
    assert "别外传" in out, "非敏感部分要保留，否则没法复核"


def test_留痕会打码_bearer():
    from core.reply_guard import redact

    out = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghij" not in out
    assert "Bearer ***" in out


def test_留痕截断并压平空白():
    from core.reply_guard import redact

    out = redact("第一行\n\n第二行   " + "x" * 300, limit=40)
    assert "\n" not in out, "审计里不该有多行，会撑坏日志"
    assert out.endswith("…")
    assert len(out) <= 41


def test_留痕不会挂掉():
    from core.reply_guard import redact

    assert redact("") == ""
    assert redact(None) == ""

