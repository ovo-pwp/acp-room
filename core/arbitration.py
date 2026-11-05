"""发言权仲裁 —— 决定"这一轮谁开口"。

## 为什么需要它

现在房间是**并集**：`ids = [a for a in (speakers or self.order) if a in self.agents]`，
不点名就全员起床。实测代价（HANDOFF.md 与 P11 记录）：一句闲聊烧 7.4 万 token、
等 4 分 20 秒，其中 OMP 一家 263 秒占 95%。而大多数时候人只想问其中一个人。

## 规则从哪来

抄 `wangshouyong/dsh-roundtable` 的确定性仲裁（`docs/DESIGN.md` §4）：

    点名独占 > 回复对象 > 职责命中 > 活跃度补位

四条按顺序试，**第一条命中就停**。没有"综合评分"，因为评分会让结果不可预测，
而不可预测的仲裁等于没有仲裁——人没法解释为什么这次是它在说话。

在此之上加一条本平台的硬约束：**`wait` 只停本轮、不插话**。
模型可以明确表示"这轮我没话说"，那就别逼它产出一段废话。

## 与现有 `@` 的关系

`web/p11-mention.js` 已经在前端解析 `@名字` 并改写 `speakers`。
本模块是它的服务端版本，两者**不冲突**：前端给了明确的 speakers 就用给的，
本模块只在"没点名"时才说话。这样既保留了前端的即时体验，
也让 API/CLI 调用（没有前端）拥有同样的仲裁能力。

## 为什么不用 LLM 选发言人

AutoGen 的 `selector` 用模型选，效果好看但有两个问题：
1. 每轮多一次模型调用，一个四人群聊的成本直接翻倍；
2. 选完说不清理由——审计里只能记"模型选的"，查不了。

确定性规则的成本是零，理由可以逐字解释，而且可测（本模块全是纯函数）。
真需要"谁更适合回答"的判断时，那应该由**人类点名**来做，而不是再加一层模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# @名字 的宽松匹配：先精确 id，再小写名字，再唯一前缀。
_AT = re.compile(r"@([A-Za-z0-9_\-\u4e00-\u9fff]+)")


@dataclass
class TurnGrant:
    """这一轮谁该说话，以及为什么。"""

    speakers: list[str]
    reason: str          # named / reply_to / role / activity / all
    detail: str = ""     # 人话解释，进审计

    def as_dict(self) -> dict:
        return {"speakers": self.speakers, "reason": self.reason, "detail": self.detail}


def resolve_at(text: str, roster: list[tuple[str, str]]) -> list[str]:
    """从正文里解析 @ 点名。roster 是 [(id, name)]。

    宽松匹配是为了少打字：`@cl` → claude。但**歧义时不猜**——
    `@c` 同时能匹配 codex 和 claude 时返回空，让人写清楚，而不是随机挑一个。
    """
    out: list[str] = []
    for tok in _AT.findall(text or ""):
        low = tok.lower()
        if low == "all":
            continue
        hit = None
        ids = [i for i, _ in roster]
        names = {i: n.lower() for i, n in roster}
        if low in ids:
            hit = low
        else:
            exact = [i for i in ids if names.get(i) == low]
            if len(exact) == 1:
                hit = exact[0]
            else:
                pref = [i for i in ids if i.startswith(low) or names.get(i, "").startswith(low)]
                if len(pref) == 1:
                    hit = pref[0]
        if hit and hit not in out:
            out.append(hit)
    return out


def arbitrate(
    *,
    text: str,
    explicit: list[str] | None,
    roster: list[tuple[str, str]],
    reply_to: str | None = None,
    mentions: list[str] | None = None,
    activity: dict[str, int] | None = None,
    max_speakers: int = 3,
) -> TurnGrant:
    """四段仲裁。返回 TurnGrant。

    参数：
      explicit  调用方明确指定的发言人（前端 p11 或 /api/run 的 speakers）。
                非空即最高优先——人已经点过名了，不要再替他决定。
      reply_to  被回复的那条发言的 agent id（前端"回复某条"时传）。
      mentions  从正文解析出的 @ 名单（可省，省了就用 text 现解析）。
      activity  {agent_id: 最近发言次数}，只在最后一步补位时用。
      max_speakers 一次最多叫几个人。超过就按上面的优先级截断——
                这不是限制自由，是防止"一句提问叫醒整个房间"。
    """
    ids = [i for i, _ in roster]

    # 0. 调用方明确指定。空列表也算"明确"——那意味着"这轮谁都别说"。
    if explicit is not None:
        keep = [a for a in explicit if a in ids]
        if keep != list(explicit):
            dropped = [a for a in explicit if a not in ids]
            return TurnGrant(keep, "explicit",
                             f"调用方指定 {explicit}，其中 {dropped} 不在房间里，已忽略")
        return TurnGrant(keep, "explicit", f"调用方指定 {keep}")

    # 1. 点名独占
    named = mentions if mentions is not None else resolve_at(text, roster)
    named = [a for a in named if a in ids]
    if named:
        return TurnGrant(named[:max_speakers], "named", f"正文里 @ 了 {named}")

    # 2. 回复对象
    if reply_to and reply_to in ids:
        return TurnGrant([reply_to], "reply_to", f"回复 {reply_to}")

    # 3. 职责命中：正文里出现某个 agent 的 id 或名字（不加 @）
    low = (text or "").lower()
    role_hits = []
    for aid, name in roster:
        if len(aid) >= 3 and re.search(rf"\b{re.escape(aid)}\b", low):
            role_hits.append(aid)
        elif name and len(name) >= 2 and name.lower() in low:
            role_hits.append(aid)
    if role_hits:
        uniq = [a for a in dict.fromkeys(role_hits)]
        return TurnGrant(uniq[:max_speakers], "role",
                         f"正文提到 {uniq}（未加 @，按职责命中）")

    # 4. 活跃度补位：最近说得最少的先来。activity 缺省时按房间顺序。
    if activity:
        ranked = sorted(ids, key=lambda a: (activity.get(a, 0), ids.index(a)))
        return TurnGrant(ranked[:max_speakers], "activity",
                         f"没有点名，按最近发言次数补位：{ranked[:max_speakers]}")
    return TurnGrant(ids[:max_speakers], "all",
                     f"没有点名也没有活跃度数据，按房间顺序取前 {max_speakers} 个")
