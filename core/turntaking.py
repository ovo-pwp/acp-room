"""发言顺序 —— 由对话本身决定，而不是按名册轮。

## 现在的行为，以及它为什么"生硬"

`parallel_turns: true`（默认）时，同一轮里所有 agent **并行**跑，跑完才落盘。
所以同一轮内 A 看不到 B 刚说的——它只能对着上一轮的旧上下文发言，
结果就是"四个人轮流总结同一个话题"，而不是"接着某人的话往下说"。

改成串行（`parallel_turns: false`）能让后者看到前者，但顺序仍然是名册固定的
`order`，而且每个人都被叫一次，说没话说都得说。

**"自由发言"要的是两件事：**
1. 下一个人由**上一句说了什么**决定（被点名的人接话），不是名册顺序
2. 谁都可以多说几次，也可以不说

## 规则（确定性，不调模型）

    1. 上一句点名了某人（@id / @名字 / 首句称呼）→ 那个人接话
    2. 上一句没点名      → 给"本场还没开过场的人"一次机会
    3. 都开过场了        → 结束（没人在接话就是话题结束）
    4. 有人达到尝试上限   → 跳过它

第 2 条原来是"选说话最少的人补位"，那**本身就是轮询**（实测日志
`pi omp claude codex` 循环 12 步）。改成"只给没开过场的人"之后，
补位就只剩"让每个人有一次立场陈述"这一个职责。

**为什么不用模型选下一个**（AutoGen 的 selector 那样）：
每轮多一次模型调用，四人群聊成本直接翻倍；而且选完说不出理由，
审计里只能记"模型选的"，事后查不了。这里全是纯函数，可测、可解释、零成本。

## 和 wait 的关系

自由发言模式下 `[WAIT]` 才真正有意义：agent 能看到别人刚说的，
所以"我没什么可补的"是一个**知情的**判断，而不是懒惰。
上一轮把 wait 收紧成"被点名必须回答"正是为了让这里能放心用它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# 上一句里的 @ 点名
_AT = re.compile(r"@([A-Za-z0-9_\-\u4e00-\u9fff]+)")

# 首句里出现的名字。**不用词表** —— 原来写死 `这轮|刚才|说得|的|：`，
# 结果「Codex 那条判据的问题出在分母」匹配不上（"那条"不在表里），
# 而这句话是实测里最常见的反驳开头。词表越写越长且永远漏，不如直接找名字。
#
# 只看**第一行**：人在段中提别人名字通常是举例，不是对话对象。
_FIRST_LINE_SPLIT = re.compile(r"[\r\n。！？!?]")

# Latin 名字要词边界，否则 "Pi" 会命中 "pipeline"、"pivot" 这类词。
_WORD_BOUNDED = "(?<![A-Za-z0-9_]){0}(?![A-Za-z0-9_])"


def _find_name_in_first_line(text: str, roster: Sequence[tuple[str, str]],
                             speaker: str) -> str | None:
    """在首句里找别人提到的人名。先出现的先算。"""
    line = _FIRST_LINE_SPLIT.split(text or "", 1)[0]
    if not line:
        return None
    low = line.lower()
    best: tuple[int, str] | None = None
    for aid, name in roster:
        if aid == speaker:
            continue
        for token in (aid, name):
            tok = str(token or "").strip()
            if len(tok) < 2:
                continue
            # Latin/数字构成的名字加词边界；纯中文名直接找（中文没有词边界概念）
            if re.fullmatch(r"[A-Za-z0-9_\-]+", tok):
                pat = re.compile(_WORD_BOUNDED.format(re.escape(tok)), re.I)
            else:
                pat = re.compile(re.escape(tok))
            m = pat.search(low)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), aid)
    return best[1] if best else None


@dataclass
class Step:
    """这一步该谁说话，以及为什么。"""

    speaker: str
    reason: str          # addressed | lead_name | activity | first
    detail: str = ""

    def as_dict(self) -> dict:
        return {"speaker": self.speaker, "reason": self.reason, "detail": self.detail}


def _norm(name: str) -> str:
    return str(name or "").strip().lower()


def match_name(token: str, roster: Sequence[tuple[str, str]]) -> str | None:
    """把一个词解析成 agent id。宽松但**歧义时不猜**。

    `@cl` → claude；`@c` 同时能匹配 codex 和 claude → 返回 None，让人写清楚。
    """
    low = _norm(token)
    if not low or low == "all":
        return None
    ids = [i for i, _ in roster]
    if low in ids:
        return low
    exact = [i for i, n in roster if _norm(n) == low]
    if len(exact) == 1:
        return exact[0]
    pref = [i for i, n in roster if i.startswith(low) or _norm(n).startswith(low)]
    return pref[0] if len(pref) == 1 else None


def parse_addressee(text: str, roster: Sequence[tuple[str, str]],
                    speaker: str = "") -> str | None:
    """这句发言在跟谁说话？找不到返回 None。

    先看 `@`（最明确），再看句首称呼。**排除自己**：agent 常写
    "我 Pi 认为…"，那不是在对 Pi 说话。
    """
    if not text:
        return None

    hits = _AT.findall(text)
    for tok in hits:
        who = match_name(tok, roster)
        if who and who != speaker:
            return who
    # 没有 @ 到别人（可能只 @ 了自己，或根本没 @）→ 看首句有没有名字
    return _find_name_in_first_line(text, roster, speaker)


def next_speaker(*, last_text: str, last_speaker: str,
                 roster: Sequence[tuple[str, str]],
                 spoke: dict[str, int],
                 attempts: dict[str, int] | None = None,
                 max_per_agent: int = 3,
                 pending: Sequence[str] = (),
                 tried: dict[str, int] | None = None) -> Step | None:
    """决定下一个发言人。返回 None 表示本场结束。

    ## 三个计数器的区别（这里是关键）

    - `spoke`：真的说过话的次数（`[WAIT]` 不算）
    - `attempts`：被叫到发言权的次数（含 `[WAIT]`）。缺省等于 `spoke`
    - `tried`：轮到过但**没说出内容**的次数（空正文/故障）。缺省空

    `attempts` 用来兜底防死循环（被点名的人一直 wait 会被反复选中）；
    `spoke` 用来判断"谁还没开过场"。

    ## 补位规则的修正（实测踩过）

    原来是"选说话最少的人补位"。这**本身就是轮询**——实测 4 人时
    日志是 `pi omp claude codex pi omp claude codex …`，
    连续 12 步 `reason` 全是 `activity`，顺序和名册一模一样。
    换句话说：换了个理由的轮询，不是自由发言。

    现在补位只做一件事：**给本场还没说过话的人一次开场机会**。
    所有人都说过之后，补位不再选人 —— 对话只能靠"被点名/被接话"延续。
    没人在接话，就是话题结束。
    """
    ids = [i for i, _ in roster]
    if not ids:
        return None
    attempts = attempts if attempts is not None else spoke
    tried = tried if tried is not None else {}

    def can(a: str) -> bool:
        return a in ids and attempts.get(a, 0) < max_per_agent

    # 0. 调用方排的队最优先（人已经点过名了，不要替他改）
    for a in pending:
        if can(a):
            return Step(a, "addressed", "调用方排在队首")

    # 1. 上一句在跟谁说话 —— 自由发言的主驱动
    who = parse_addressee(last_text, roster, speaker=last_speaker)
    if who and can(who):
        return Step(who, "addressed", f"上一句在跟 {who} 说话")
    detail_limit = ""
    if who:
        detail_limit = f"{who} 已达上限 {max_per_agent}，改看还有谁没开过场"

    # 2. 补位：只找**本场还没真正说过话**的人
    #
    # `tried` 是"已经轮到过、但没说出内容"的人（空正文/故障）。
    # 把整场预算烧在一个人身上——实测 pi 连续两次 `opening: 还没开过场`。
    # 补位的本意是"让还没轮到的人有机会"，不是"让说空话的人说到说出来为止"。
    fresh = [a for a in ids if spoke.get(a, 0) == 0 and not tried.get(a, 0) and can(a)]
    if fresh:
        pick = min(fresh, key=lambda a: attempts.get(a, 0))
        why = f"还没开过场，轮到 {pick}"
        if detail_limit:
            why = detail_limit + "；" + why
        return Step(pick, "opening", why)

    # 3. 都开过场了，也没人接话 → 结束
    return None


def opening(*, roster: Sequence[tuple[str, str]], activity: dict[str, int],
            addressed: Sequence[str] = ()) -> Step | None:
    """本场第一句谁先说。

    和 `next_speaker` 的区别：这里没有"上一句"，所以只看点名和活跃度。
    `activity` 是最近发言次数——说得最少的人先开，避免总是同一个开头。
    """
    ids = [i for i, _ in roster]
    if not ids:
        return None
    for a in addressed:
        if a in ids:
            return Step(a, "addressed", "人类点名由它开场")
    least = min(activity.get(a, 0) for a in ids)
    pick = next(a for a in ids if activity.get(a, 0) == least)
    return Step(pick, "first", f"没人点名，最近说得最少的 {pick} 开场（{least} 次）")


def went_flat(history: Sequence[str], roster: Sequence[tuple[str, str]]) -> bool:
    """连续一整轮没人接别人的话 → 话题已经走平，该收。

    ## 为什么需要它

    实测：4 人、rounds=3、max_per_agent=3 时，循环会**走满 12 步**
    （日志里 第1步pi 第2步omp 第3步claude 第4步codex 第5步pi…），
    因为补位规则保证"说话最少的人"总有额度，于是轮询被强制执行到底。
    那不是自由发言，是**换了个顺序的轮询**。

    真实对话结束的信号是：没人再接别人的话。所以连续 `len(roster)` 次
    补位（而不是点名/接话）出现时，判定话题走平。

    `history` 是每一步的 reason；`"addressed"` 表示有人在接话，
    任何非 addressed 的一步都让计数归零。
    """
    n = max(2, len(roster))
    if len(history) < n:
        return False
    return all(r != "addressed" for r in history[-n:])


def budget_steps(n_agents: int, rounds: int, max_per_agent: int = 3) -> int:
    """自由发言的总步数上限。

    `rounds` 在界面上是"轮数"，在自由模式里换算成"大概能说几轮的量"：
    每轮名义上每人说一次，再留一点余量给"被点名的人多说一次"。
    必须设上限——链式接话可能自己绕圈（A 问 B，B 反问 A）。
    """
    n = max(1, int(n_agents))
    r = max(1, int(rounds))
    return min(n * r + max(2, n), n * max_per_agent * 2)


@dataclass
class TurnBatch:
    """下一步：一个 batch 里的人**并行**跑。

    为什么允许并行：一句「@pi @omp 你们俩怎么看」同时点了两个人，这两个人之间
    **没有先后依赖**——他们看的都是同一份上文。并行跑等于把两条互不相干的等待
    叠成一条，实测 33.2s + 18.7s 变 33.2s。

    有依赖的那些（上一句在跟某人说话，下一句要接他的话）仍然是一个人一步，串行。
    """

    speakers: list[str]
    reason: str          # batch | addressed | opening
    detail: str = ""     # 人话解释，进审计

    def as_dict(self) -> dict:
        return {"speakers": self.speakers, "reason": self.reason, "detail": self.detail}


def parse_batch(text: str, roster: Sequence[tuple[str, str]],
                speaker: str = "") -> list[str]:
    """一句里被 @ 到的**所有人**（去重、排除说话者自己、歧义的名字丢掉）。

    和 `parse_addressee` 的区别是那个只回第一个 —— 那是"接话"语义（一个人往下说），
    这里要的是"这句话同时点了谁"。两个都得留着：拿第一个当批次，就会把
    「@pi @omp」的批变成只有 pi， omp 白白没被叫。
    """
    if not text:
        return []
    out: list[str] = []
    for tok in _AT.findall(text):
        who = match_name(tok, roster)
        if who and who != speaker and who not in out:
            out.append(who)
    return out


def next_step(*, last_text: str, last_speaker: str,
              roster: Sequence[tuple[str, str]],
              spoke: dict[str, int],
              attempts: dict[str, int] | None = None,
              max_per_agent: int = 3,
              tried: dict[str, int] | None = None,
              pending: Sequence[str] = ()) -> TurnBatch | None:
    """这一步该哪些人说话。返回 None = 本场结束。

    规则还是 `next_speaker` 那三条（点名 / 补位 / 都开过场就结束），只是把
    "一句 @ 到多个人" 单独抬成一步并行 —— 判定全在纯函数里，不调模型、可测、可解释。
    """
    step = next_speaker(
        last_text=last_text, last_speaker=last_speaker, roster=roster,
        spoke=spoke, attempts=attempts, max_per_agent=max_per_agent,
        tried=tried, pending=pending)
    if step is None:
        return None
    ids = [i for i, _ in roster]

    def can(a: str) -> bool:
        return (a in ids
                and (attempts if attempts is not None else spoke).get(a, 0) < max_per_agent
                and not (tried or {}).get(a, 0))

    # 只有"上一句真的 @ 了至少两个人"才成批。补位/接话一律一步一个人。
    # `pending` 非空时更不成批：那是调用方（人点名 / @all）已经排好队了，
    # 替他改成并行等于改了发言名单——审计里会显示"这两个人同时被叫"，
    # 而实际是那个人叫的，事后查不到谁的决定。
    pair = parse_batch(last_text, roster, speaker=last_speaker)
    if len(pair) > 1 and step.reason == "addressed" and not pending:
        batch = [a for a in pair if can(a)]
        if len(batch) > 1:
            return TurnBatch(batch, "batch", f"上一句同时点到 {'、'.join(batch)}，并行跑")
    return TurnBatch([step.speaker], step.reason, step.detail)
