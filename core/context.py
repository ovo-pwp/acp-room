"""上下文组装 —— 纯函数，不 import 任何宿主类型。

## 为什么从 room.py 抽出来

`Room._prompt_for` 原来和 `self.transcript`、`self.seen`、`self.skills` 绑在一起，
想验证"分层压缩到底压哪几条"就必须起一个 Room（连带 4 个 ACP 进程）。
而这个判断本身是纯函数——给定一段发言列表和一个游标，输出一段字符串。

抽出来之后：
  - 可以直接单测（`tests/test_context.py`）
  - 压哪几条、压多少字这些参数变成显式入参，不再藏在方法体里
  - `dsh-roundtable` 的约束"core 不 import 宿主"在这里被遵守

## 分层的边界为什么是这两条

    NEAR = 6       近 6 条全文
    COMPRESS = 120 更早的压到 120 字

实测依据：一次讨论 5 阶段 × 4 人 = 20 条，一个 agent 每轮只看"自己上次发言之后"的
几条，通常 0~4 条；6 条覆盖绝大多数正常轮次，只有长时间没人说话（或它自己被 wait 掉
好几轮）才会触发压缩。120 字大约是一段结论的长度，再短就连"它反对什么"都看不清了。

**不做 LLM 摘要**：那要额外一次调用，而且会引入"摘要里的错误"这种查不出来的失真。
规则压缩的损失是可预期的，而且每个 agent 自己的 ACP session 里本来就有完整上下文，
这里只是给"没在场的人"一个可读的交代。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

# 近 N 条给全文
NEAR = 6
# 更早的单条压到多少字
COMPRESS = 120
# 单条全文上限（防止一条超长发言把窗口吃光）
HARD_CAP = 4000


class TurnLike(Protocol):
    """只要求三个属性。不 import room.Turn，避免 core → 宿主 的反向依赖。"""

    speaker: str
    name: str
    text: str


@dataclass
class ContextPlan:
    """组装结果 + 可观测的元数据（进审计用）。"""

    body: str
    total: int        # 参与组装的发言条数
    compressed: int   # 被压缩的条数
    truncated: int    # 被硬截断的条数

    def as_dict(self) -> dict[str, int]:
        return {"总条数": self.total, "已压缩": self.compressed, "已截断": self.truncated}


def pending_since(transcript: Sequence[TurnLike], start: int, self_id: str) -> list[TurnLike]:
    """取「我上次发言之后、别人说的」那几条。

    自己的话要排除：agent 自己的发言在它的 ACP session 里已经有了，
    再推一遍等于让它读自己的话，白烧 token 还容易让它复读。
    """
    if start <= 0:
        seg = list(transcript)
    else:
        seg = list(transcript[start:])
    return [t for t in seg if getattr(t, "speaker", "") != self_id]


def build_body(pending: Sequence[TurnLike], note: str,
               near: int = NEAR, compress: int = COMPRESS,
               hard_cap: int = HARD_CAP) -> ContextPlan:
    """把一段发言拼成 prompt 正文（不含房间规则 / 技能块 / wait 提示）。"""
    if not pending and not note:
        return ContextPlan("（上一轮没有人说话。你可以继续推进自己的观点，或者提一个具体的问题。）", 0, 0, 0)
    if not pending:
        return ContextPlan("（没有新内容，见下面的主持人附言。）", 0, 0, 0)

    cut = max(0, len(pending) - near)
    lines: list[str] = []
    if cut:
        lines.append(f"（以下 {cut} 条较早发言已压缩）")

    compressed = truncated = 0
    for i, t in enumerate(pending):
        text = (getattr(t, "text", "") or "").strip()
        if not text:
            continue
        if i >= cut:
            if len(text) > hard_cap:
                text = text[:hard_cap] + "\n…（截断）"
                truncated += 1
        elif len(text) > compress:
            text = text[:compress] + "…（已压缩）"
            compressed += 1
        lines.append(f"[{getattr(t, 'name', '?')}]\n{text}")

    return ContextPlan("\n\n".join(lines), len(pending), compressed, truncated)


def image_note(attachments: Sequence[dict]) -> str:
    """本轮新出现图片的一行说明。

    图片本体走 ACP image 内容块，正文里只留一行——
    否则模型会把"我看到了图"和"图里有什么"搞混，甚至复述这行说明当答案。
    """
    imgs = [a for a in (attachments or []) if a]
    if not imgs:
        return ""
    names = "、".join((a.get("name") or a.get("id", "")) for a in imgs[:5])
    more = f" 等 {len(imgs)} 张" if len(imgs) > 5 else ""
    return f"\n\n[附带 {len(imgs)} 张图片：{names}{more}]"


def identity_block(self_name: str, self_id: str,
                   roster: Sequence[tuple] = (),
                   roles: dict[str, tuple[str, str]] | None = None,
                   self_role: str = "", self_tag: str = "",
                   addressed: bool = False) -> str:
    """告诉 agent「你是谁」。

    ## 为什么必须每轮都发

    实测（2026-11-03）：人类写 `@omp`，omp 回了 `wait`，理由是
    「这条点名 @omp，不是我」——**它不知道自己的名字叫 omp**。
    平台从没在任何地方告知过它。于是 P11 的 @ 路由正确地把发言权只给了 omp，
    结果 omp 自己把这一轮拒了。审计里 `turn_grant` 与 `turn_wait` 各 6 条一一对应。

    ## 实现已合并到 core/persona.py

    这里只做转发。合并的原因：本文件先有一个"只带名字"的版本，
    `core/persona.py` 后来又做了"带角色/人格"的版本。两份身份实现并存会让人
    分不清哪份在生效，而不生效的那份会悄悄腐烂（没有测试会覆盖它）。

    `roster` 兼容 `[(id, name)]` 与 `[(id, name, tag)]` 两种形状。
    `roles` 是 `{id: (role, tag)}`，给需要角色文本的调用方。`addressed=True`
    表示这轮被点名，会追加"必须回答"的硬要求。
    """
    from core import persona  # 局部导入：避免 context ↔ persona 的循环
    me, others = persona.from_roster(
        self_id, self_name, roster, roles,
        self_role=self_role, self_tag=self_tag)
    return persona.build(me, others, addressed=addressed)


def assemble(preamble: str, skill_head: str, plan: ContextPlan,
             note: str = "", wait_hint: str = "",
             attachments: Sequence[dict] | None = None,
             self_name: str = "", self_id: str = "",
             roster: Sequence[tuple] = (),
             roles: dict[str, tuple[str, str]] | None = None,
             self_role: str = "", self_tag: str = "",
             addressed: bool = False,
             memory: str = "") -> str:
    """最终拼装。顺序固定：房间规则 → 身份 → 往期结论 → 技能块 → 正文(+图片说明) → 主持人 → wait 提示。

    顺序不是随意的：
      - 房间规则在最前，因为它要压过 agent 自带的"我是编程助手"人设
      - 身份紧随其后，回答"我在跟谁说话、我是谁"（实测缺了它 @ 点名会被拒）
      - 往期结论在身份之后、`---` 之前：它是"你是谁/你在哪"的一部分（背景），
        不是"这一轮的内容"。放在分隔线之后会被当成议题本身。
      - 技能块跟着，是"这一轮可用什么"的声明
      - wait 提示在最后，离"开始输出"越近越容易被遵守
    """
    body = plan.body + image_note(attachments or [])
    tail = f"\n\n[主持人]\n{note}" if note else ""
    tail += wait_hint
    ident = (identity_block(self_name, self_id, roster, roles,
                            self_role=self_role, self_tag=self_tag,
                            addressed=addressed)
             if self_id else "")
    mid = (ident + "\n\n") if ident else ""
    mid += memory or ""
    return preamble + "\n\n" + mid + "---\n\n" + (skill_head or "") + body + tail


def pick_topic(transcript: Sequence[TurnLike]) -> str:
    """最近一条有人类文字的发言，作为技能检索的 query。"""
    for t in reversed(list(transcript)):
        if getattr(t, "speaker", "") == "human" and (getattr(t, "text", "") or "").strip():
            return t.text
    return ""


def activity(transcript: Sequence[TurnLike], ids: Sequence[str], window: int = 30) -> dict[str, int]:
    """最近 window 条里每人说了几次。仲裁补位时用。"""
    out = {i: 0 for i in ids}
    for t in list(transcript)[-window:]:
        sp = getattr(t, "speaker", "")
        if sp in out:
            out[sp] += 1
    return out


def last_human_text(transcript: Sequence[TurnLike]) -> str:
    for t in reversed(list(transcript)):
        if getattr(t, "speaker", "") == "human":
            return getattr(t, "text", "") or ""
    return ""


def wait_reason(text: str, pattern: Any) -> str | None:
    """"判定这条输出是不是 wait。返回理由（可能是空串），不是就返回 None。

    pattern 由调用方传入（`room.py` 的 `_WAIT_RE`），避免 core 里再定义一份正则
    导致两边行为漂移。
    """
    if not text:
        return None
    if not pattern.match(text):
        return None
    return pattern.sub("", text).strip()[:80] or "（未说明理由）"
