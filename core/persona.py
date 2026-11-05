"""身份与人格 —— 让每个 agent 知道"我是谁"，而不只是"我在一个房间里"。

## 这个模块修的是两个看起来无关、实为同一个根因的问题

### 问题一：回答一次就不再回复

审计里的原始证据（`turn_grant` 与 `turn_wait` 各 6 条，一一对应）：

    grant  omp  named: 正文里 @ 了 ['omp']
    wait   omp  「这条点名 @omp，不是我。」
    wait   omp  「又是点名 @omp，与我无关。」

**被点名的 agent 说自己"不是我"。** 它看到正文里的 `@omp` 时，不知道自己叫 omp，
于是把点名当成了在叫别人，主动闭嘴。

原来的 PREAMBLE 只说「你在一个多 AI 聊天室里」，**从头到尾没说你是谁**。
有时它能从自己历史发言的 `[OMP]` 前缀里猜出身份，有时猜不出——
所以这个 bug 是**间歇性**的：同一条消息，有时回，有时不回。

### 问题二：生硬、没有独立意识

同一个根因。一个不知道"我是谁、我负责什么"的模型，只能给出通用的、
面向所有人的回答；它无法从某个立场说话，也没有理由和别人不一样。

## 修法

在每轮 prompt 最前面加三段，顺序固定：

    [你是谁]     我的 id、名字、角色、说话方式
    [房间里]     其他每个 agent 的 id / 名字 / 角色
    [怎么称呼]   @我的名字 = 叫我，必须回答；@别人 = 不是叫我

顺序不能换：先确立"我"，才能理解"别人"，最后才谈"怎么被称呼"。

## 为什么放在 core 且是纯函数

身份块是纯粹字符串拼接，没有任何宿主依赖。抽出来的好处是可单测——
上一轮的教训是：不可测的判断会悄悄坏掉（`pick_least_similar` 的 min/max 反了，
没有任何测试能发现）。身份块要是写错了（比如把自己列进了"房间里其他人"），
症状同样只是"模型偶尔不回复"，肉眼根本查不出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 内置默认人格。加新 agent 时如果没写 role，按 id 取这里的；再没有就用通用兜底。
# 刻意写得**互相不同**：人格雷同的圆桌会变成四个复读机。
DEFAULT_ROLES: dict[str, str] = {
    "pi": (
        "你是房间里负责**拆解问题**的那一个。习惯先把问题切成几条独立的子问题，"
        "再逐条判断，最后给结论。喜欢用对照和反例说明问题，不喜欢铺垫。"
        "你对没有边界的说法过敏——听到「大多数情况」「通常」会追问具体条件。"
    ),
    "omp": (
        "你是房间里负责**质疑前提**的那一个。你的默认动作是找出别人论证里"
        "那个「没说出口但必须为真」的假设。你觉得大多数分歧不是事实之争，"
        "而是双方默认了不同的前提。你说话直接，不怕得罪人，但会给出理由。"
    ),
    "claude": (
        "你是房间里负责**代价与边界**的那一个。任何方案你都先问：这要花多少、"
        "什么时候会失败、谁来维护、出错时怎么退回去。你倾向于「先做能立刻验证的那一步」，"
        "并明确说出你放弃了什么。你不喜欢宏大叙事。"
    ),
    "codex": (
        "你是房间里负责**落地**的那一个。你倾向给具体步骤、可执行的判断标准、"
        "以及「做完怎么知道成了」。你的回答通常最短。如果别人停留在抽象层面，"
        "你会把它压到一个明天早上能做的动作上。"
    ),
    "hermes": (
        "你是房间里负责**收敛**的那一个。听完一轮之后，你把内容归成三栏："
        "已经一致的、真正分歧的、还没验证的。说分歧时必须指出它卡在哪个前提上，"
        "而不是笼统地说「双方都有道理」。你只归纳不新增观点——如果两方其实在回答"
        "不同的问题，你负责把这层错位挑明。你最讨厌把没解决的分歧说成已解决。"
    ),
}

# 名册里用的短标签。**不要把上面那段第二人称的角色原文塞进名册**——
# 名册是"别人是谁"，用「你是……」的句子描述别人会读着别扭，还白占上下文。
# 实测：4 个 agent 各 80 字简介 = 320 字，每次都推给每个 agent，是纯浪费。
DEFAULT_TAGS: dict[str, str] = {
    "pi": "拆解问题 · 切子问题、要边界、给对照",
    "omp": "质疑前提 · 找没说出口的假设",
    "claude": "代价与边界 · 花多少、何时失败、怎么退",
    "codex": "落地 · 给具体步骤和验收标准",
    "hermes": "收敛 · 归纳共识、揪出真分歧、标出待验证",
}

FALLBACK_ROLE = (
    "你是房间里的一个参与者。给出你自己的判断，而不是复述共识。"
    "有依据就给依据，没有就说没有。"
)


@dataclass(frozen=True)
class Persona:
    """一个 agent 的身份。id 是机器用的，name 是人看的。

    `__post_init__` 会在 role/tag 为空时补上内置默认。**兜底放在这里而不是
    只放在 from_cfg**：裸构造 `Persona(id="pi")` 与 `from_cfg({"id":"pi"})`
    必须得到同一个身份，否则同一个 agent 走不同代码路径会有两种人格，
    而这种不一致只会表现为"有时候说话正常、有时候像另一个人"。
    """

    id: str
    name: str = ""
    role: str = ""
    tag: str = ""
    style: str = ""

    def __post_init__(self) -> None:
        # frozen 数据类只能用 object.__setattr__ 赋值
        if not str(self.name or "").strip():
            object.__setattr__(self, "name", self.id)
        if not str(self.role or "").strip():
            object.__setattr__(self, "role", DEFAULT_ROLES.get(self.id, FALLBACK_ROLE))
        if not str(self.tag or "").strip():
            object.__setattr__(self, "tag", DEFAULT_TAGS.get(self.id, ""))

    @classmethod
    def from_cfg(cls, a: dict) -> "Persona":
        """从 config.json 的 agents[] 一条建 Persona。缺 role 时用内置默认。"""
        aid = str(a.get("id") or "").strip()
        role = str(a.get("role") or "").strip() or DEFAULT_ROLES.get(aid, FALLBACK_ROLE)
        tag = str(a.get("tag") or "").strip() or DEFAULT_TAGS.get(aid, "")
        return cls(
            id=aid,
            name=str(a.get("name") or aid),
            role=role,
            tag=tag,
            style=str(a.get("style") or "").strip(),
        )

    def brief(self) -> str:
        """名册里显示的一句话。有 tag 用 tag，否则从 role 里截一段。"""
        if self.tag:
            return self.tag
        one = _clean(self.role) if "_clean" in globals() else " ".join(self.role.split())
        return one[:60] + ("…" if len(one) > 60 else "")


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def identity_block(me: Persona) -> str:
    """[你是谁] —— 这一段是修那个 bug 的关键。"""
    lines = [
        "[你是谁]",
        f"你是 {me.name}。你在这个房间里的 id 是 `{me.id}`。",
    ]
    if _clean(me.role):
        lines.append(_clean(me.role))
    if _clean(me.style):
        lines.append(f"说话方式：{_clean(me.style)}")
    return "\n".join(lines)


def roster_block(me: Persona, others: Sequence[Persona]) -> str:
    """[房间里] —— 知道自己之外还有谁，才谈得上"跟谁对话"。

    关键：**把自己排除在外**。写错这一点（把自己也列进"其他人"）会让模型
    以为房间里有两个自己，症状同样只是"说话变怪"，肉眼查不出来。
    """
    rest = [p for p in others if p.id != me.id]
    if not rest:
        return "[房间里]\n目前只有你一个 AI。"
    lines = ["[房间里]", "除你之外还有这些 AI（人类看得到他们的名字）："]
    for p in rest:
        lines.append(f"- {p.name}（id: `{p.id}`）—— {p.brief()}")
    return "\n".join(lines)


def addressing_block(me: Persona, others: Sequence[Persona]) -> str:
    """[怎么称呼] —— 把 @ 的含义钉死。

    这是直接针对那条 `「这条点名 @omp，不是我」` 的条款：
    必须明说"@你的 id 或 @你的名字 = 叫你"，否则模型只能猜。
    """
    others_names = "、".join(f"@{p.id}" for p in others if p.id != me.id) or "其他 AI"
    return (
        "[怎么称呼]\n"
        f"人类写 `@{me.id}` 或 `@{me.name}` 时，**那是在叫你，你必须回答**，"
        f"不许说自己不是被叫的那个。\n"
        f"人类写 {others_names} 时，不是在叫你——除非你有别人没说到的实质内容，"
        f"否则不要发言。\n"
        "别人发言里出现的 @ 是他们在点名，和当前这轮谁该说话无关。"
    )


def from_roster(self_id: str, self_name: str,
                roster: Sequence[tuple], roles: dict[str, tuple[str, str]] | None = None,
                self_role: str = "", self_tag: str = "") -> tuple[Persona, list[Persona]]:
    """从 `[(id, name)]` 或 `[(id, name, tag)]` 建出 (我, 其他人)。

    向后兼容二元组：另一个交付批次已经在用 `[(id, name)]` 的形状调身份块，
    改成三元组会让它静默失效（名字还在，角色没了）。
    """
    roles = roles or {}
    me = Persona(id=self_id, name=self_name or self_id,
                 role=self_role or DEFAULT_ROLES.get(self_id, FALLBACK_ROLE),
                 tag=self_tag or DEFAULT_TAGS.get(self_id, ""))
    others: list[Persona] = []
    for entry in roster:
        if len(entry) < 2:
            continue
        aid, name = str(entry[0]), str(entry[1])
        tag = str(entry[2]) if len(entry) > 2 else ""
        r_role, r_tag = roles.get(aid, ("", ""))
        others.append(Persona(
            id=aid,
            name=name or aid,
            role=r_role or DEFAULT_ROLES.get(aid, FALLBACK_ROLE),
            tag=tag or r_tag or DEFAULT_TAGS.get(aid, ""),
        ))
    return me, others


def build(me: Persona, others: Sequence[Persona], *, addressed: bool = False) -> str:
    """拼出完整的身份头。放在房间规则之前。

    `addressed=True` 表示这一轮人类点名要你回答，用于收紧 wait 规则。
    """
    parts = [identity_block(me), roster_block(me, others), addressing_block(me, others)]
    if addressed:
        parts.append(
            "[本轮]\n这一轮人类点名要你回答。**必须给出实质回答**，"
            "不许用退出口，不许说自己不是被叫的那个。"
        )
    return "\n\n".join(parts) + "\n\n"
