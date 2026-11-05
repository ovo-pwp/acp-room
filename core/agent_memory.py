"""把房间的结论推进 **agent 自己的**长期记忆文件。

## 为什么房间记忆不够

`core/memory.py` 存的是房间侧的结论，注入方式是每轮随 prompt 发过去
（`core/context.py` 的 `mid += memory`）。agent 一关、会话一换，那份东西
就不在它脑子里了 —— 它只是"这轮读到了"，不是"它记得"。

各家 CLI 都有自己的长期记忆落点，开机/新会话时自动加载。这个模块只做一件事：
把结论写进那些落点，让结论在 agent 侧也活下来。

## 各家落点是怎么定下来的（2026-11-04 逐个查证，不是猜的）

| agent | 落点 | 依据 |
|---|---|---|
| claude | `~/.claude/CLAUDE.md` | Claude Code 的 user 级记忆文件，每次会话加载 |
| codex | `~/.codex/AGENTS.md` | Codex 的全局 AGENTS.md（本机已存在，0 字节） |
| pi | `~/.pi/agent/AGENTS.md` | pi 的配置根下找 context file |
| omp | `~/.agents/AGENTS.md` | `discovery/agents.ts:124` `getUserPathCandidates` = `$HOME` × `.agent[s]` |
| hermes | `~/AppData/Local/hermes/memories/MEMORY.md` | `tools/memory_tool_store.py:26` 的 `§` 条目存储 |

前四个是同一种东西：**instructions 文件**（markdown，我们只在自管的标记块内改）。
Hermes 完全不同：那是它的 **curated memory store**，条目按 `\n§\n` 分隔、
有整份预算（`memory_char_limit: 2200`）、加载时做威胁扫描，
还会因为"一条比整份预算还长的自由文本"判定文件漂移而拒绝写入。
所以格式必须是它自己的格式，不能塞 markdown 块。

## 三条硬规矩

1. **不创建别人的家目录**。落点的父目录不存在 = 这个 agent 没装/没在用，
   直接跳过并说明原因。给用户新建一个 `~/.claude/` 这种事不该由我做。
2. **只动自己那块**。markdown 写法是"标记块内整体替换，块外一字不改"；
   Hermes 写法是"只删以 `[acp-room]` 开头的条目"。用户手写的条目永不覆盖，
   预算不够时先丢我们自己的老条目，仍不够就拒绝写入并说明 ——
   精简别人的记忆不是我的权限。
3. **写前先备份**，备份落在房间自己的 `data/<room>/agent-memory/` 下。
   不往 agent 目录里扔 `.bak`（那些目录是别人的， litter 一旦留下就没人清）。

## 生效时机要说实话

写进去 ≠ 现在这场会话就记得。instructions 文件在**新会话**加载，
Hermes 的 store 在 `agent_init` / `system_prompt` 里 `load_from_disk()`。
所以界面和审计都按"下次会话生效"来说，不能写成"它已经记住了"。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 我们写进 markdown 的块用这对标记圈起来，块内整体替换
MARK_BEGIN = "<!-- acp-room:begin（房间讨论结论，由 acp-room 自动维护；块内内容每次写入会整体替换） -->"
MARK_END = "<!-- acp-room:end -->"
# Hermes store 里我们的条目都以此开头 —— 删旧条目时靠它认，别的条目一概不碰
HERMES_MARK = "[acp-room]"
# Hermes 的分隔符与预算（config.yaml 读不到时用这个默认值）
ENTRY_DELIMITER = "\n§\n"
HERMES_LIMIT_DEFAULT = 2200
# 一条注入多少字。文件里的条目比 prompt 注入更该留全一点，但也不能没边。
ENTRY_CHARS = 320
# 每个落点保几条
KEEP = 6


@dataclass
class Target:
    """一个 agent 的长期记忆落点。path=None 表示这台机器上没有可写的落点。"""

    agent: str
    path: Path | None
    fmt: str            # "markdown" | "hermes"
    why: str = ""       # path=None 时的原因（界面和审计都要能说出为什么没写）

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "path": str(self.path) if self.path else "",
                "format": self.fmt, "exists": bool(self.path), "why": self.why}


@dataclass
class Outcome:
    agent: str
    target: str
    status: str         # written | unchanged | skipped | failed
    detail: str
    backup: str = ""
    chars: str = ""     # hermes 用 "2,190/2,200" 这种形态报预算

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "target": self.target, "status": self.status,
                "detail": self.detail, "backup": self.backup, "chars": self.chars}


def _home(env_home: str = "") -> Path:
    return Path(env_home or os.path.expanduser("~"))


def hermes_home(home: Path, env: dict[str, str] | None = None) -> Path | None:
    """找 Hermes 的家。必须已经有 `memories/` 才算 —— 没装就不该凭空造目录。"""
    env = env if env is not None else dict(os.environ)
    cands: list[Path] = []
    if env.get("HERMES_HOME"):
        cands.append(Path(env["HERMES_HOME"]))
    cands += [home / "AppData" / "Local" / "hermes", home / ".hermes"]
    for c in cands:
        if (c / "memories").is_dir():
            return c
    return None


class AgentMemory:
    """落点解析 + 写入 + 备份。纯文件操作，不 import room，不碰 ACP。"""

    def __init__(self, root: Path | str, room: str = "main",
                 cfg: dict | None = None, home: str = "",
                 agent_ids: list[str] | None = None) -> None:
        cfg = cfg or {}
        self.dir = Path(root) / "data" / room / "agent-memory"
        self.cfg = cfg
        # 这是**自动路径**的开关（讨论跑完自己写），默认关。
        # 界面上那颗按钮不受它管：人按下去本身就是同意。
        # 名字叫 auto_promote 不叫 enabled，是因为两种发起人确实不一样，
        # 起一个含糊的名字会让人以为"关掉它就绝对不会写文件"，而按钮照样在写。
        self.auto = bool(cfg.get("auto_promote"))
        self.keep = int(cfg.get("keep") or KEEP)
        self.chars = int(cfg.get("entry_chars") or ENTRY_CHARS)
        self.h = _home(home)
        ids = agent_ids if agent_ids is not None else list(self._known_targets())
        # config 里可以覆盖落点（换安装位置/换 id 时用）：
        #   "targets": {"pi": "~/.pi/agent/AGENTS.md", "other": {"path": ..., "format": "hermes"}}
        self.overrides = cfg.get("targets") or {}
        self.agent_ids = ids

    # ---------- 落点 ----------

    @staticmethod
    def _known_targets() -> list[str]:
        return ["claude", "codex", "pi", "omp", "hermes"]

    def target_for(self, aid: str) -> Target:
        """解析一个 agent 的落点。父目录不存在 → path=None，并说清为什么。"""
        ov = self.overrides.get(aid)
        if ov:
            spec = {"format": "markdown", **(ov if isinstance(ov, dict) else {"path": ov})}
            p = Path(str(spec["path"]).replace("~", str(self.h)))
            fmt = str(spec.get("format") or "markdown")
            if not p.parent.is_dir():
                return Target(aid, None, fmt, f"配置的落点目录不存在：{p.parent}")
            return Target(aid, p, fmt)

        if aid == "claude":
            return self._file(aid, self.h / ".claude" / "CLAUDE.md", "markdown",
                              missing="没有 ~/.claude 目录（Claude Code 没装或没跑过）")
        if aid == "codex":
            return self._file(aid, self.h / ".codex" / "AGENTS.md", "markdown",
                              missing="没有 ~/.codex 目录（Codex 没装或没跑过）")
        if aid == "pi":
            return self._file(aid, self.h / ".pi" / "agent" / "AGENTS.md", "markdown",
                              missing="没有 ~/.pi/agent 目录（pi 没装或没跑过）")
        if aid == "omp":
            # omp 认 $HOME 下的 .agent/ 或 .agents/（discovery/agents.ts 的
            # getUserPathCandidates）。本机只有 .agents，且 .agent 不存在 ——
            # 挑已存在的那个写，两个都不存在就报第一个（.agent，那是它优先读的）。
            for base in (".agents", ".agent"):
                cand = self.h / base / "AGENTS.md"
                if cand.parent.is_dir():
                    return Target(aid, cand, "markdown")
            return Target(aid, None, "markdown",
                          "既没有 ~/.agents 也没有 ~/.agent 目录（omp 没装或没跑过）")
        if aid == "hermes":
            hh = hermes_home(self.h)
            if hh is None:
                return Target(aid, None, "hermes",
                              "没找到 Hermes 的 memories 目录（没装或记忆功能没启用）")
            return Target(aid, hh / "memories" / "MEMORY.md", "hermes")
        return Target(aid, None, "markdown",
                      f"我不知道 {aid} 的长期记忆写在哪个文件，需要你配 agent_memory.targets")

    def _file(self, aid: str, p: Path, fmt: str, missing: str) -> Target:
        if not p.parent.is_dir():
            return Target(aid, None, fmt, missing)
        return Target(aid, p, fmt)

    def targets(self) -> list[Target]:
        return [self.target_for(a) for a in self.agent_ids]

    # ---------- 读状态（给界面用） ----------

    def status(self) -> list[dict[str, Any]]:
        out = []
        for t in self.targets():
            d = t.as_dict()
            d["present"] = False
            d["chars"] = ""
            if t.path and t.path.exists():
                try:
                    raw = t.path.read_text(encoding="utf-8")
                except OSError as exc:
                    d["why"] = f"读不了：{exc}"
                    out.append(d)
                    continue
                if t.fmt == "hermes":
                    d["present"] = bool(self._hermes_entries(raw))
                    d["chars"] = self._hermes_usage(raw, t.path)
                else:
                    d["present"] = MARK_BEGIN in raw
                    ours = self._markdown_entries(raw)
                    d["count"] = len(ours)
                d["size"] = len(raw)
            out.append(d)
        return out

    # ---------- 写 ----------

    def promote(self, rows: list[dict], agents: list[str] | None = None) -> list[dict]:
        """rows 来自 `Memory.recent()`（新的在前）。返回每个 agent 一条 Outcome。"""
        rows = [r for r in (rows or []) if (r.get("text") or "").strip()]
        ids = agents if agents is not None else self.agent_ids
        if not rows:
            return [Outcome(a, "", "skipped", "房间记忆里还没有结论可写").as_dict()
                    for a in ids]
        return [self._promote_one(a, rows) for a in ids]

    def _promote_one(self, aid: str, rows: list[dict]) -> dict:
        t = self.target_for(aid)
        if t.path is None:
            return Outcome(aid, "", "skipped", t.why).as_dict()
        try:
            if t.fmt == "hermes":
                res = self._write_hermes(t, rows)
            else:
                res = self._write_markdown(t, rows)
        except Exception as exc:      # noqa: BLE001 - 写别人的文件失败不该把讨论卡住
            return Outcome(aid, str(t.path), "failed", f"写入异常：{exc}").as_dict()
        return res.as_dict()

    def _lines(self, rows: list[dict], chars: int) -> list[str]:
        """条目文本，新的在前 → 落到文件里也按新的在前，读的人先看最近的。"""
        out = []
        for r in rows[:self.keep]:
            one = " ".join(str(r.get("text") or "").split())
            if len(one) > chars:
                one = one[:chars] + "…"
            day = time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or time.time())))
            topic = (r.get("topic") or "").strip()
            head = f"{day} 「{topic}」" if topic else day
            out.append((head, one))
        return out

    # ----- markdown 类落点 -----

    @staticmethod
    def _markdown_entries(raw: str) -> list[str]:
        m = re.search(re.escape(MARK_BEGIN) + r"(.*?)\n?" + re.escape(MARK_END),
                      raw, re.S)
        if not m:
            return []
        return [ln.strip() for ln in m.group(1).splitlines() if ln.strip().startswith("- ")]

    def _write_markdown(self, t: Target, rows: list[dict]) -> Outcome:
        items = self._lines(rows, self.chars)
        body = "\n".join(f"- {head}：{txt}" for head, txt in items)
        block = f"{MARK_BEGIN}\n## 往期的讨论结论（acp-room）\n\n{body}\n{MARK_END}"
        if t.path.exists():
            raw = t.path.read_text(encoding="utf-8")
            if MARK_BEGIN in raw and MARK_END in raw:
                new = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END),
                             lambda _m: block, raw, count=1, flags=re.S)
            else:
                sep = "" if raw.endswith("\n\n") else ("\n\n" if raw.endswith("\n") else "\n\n")
                new = raw + sep + block + "\n"
        else:
            new = "# 我的长期记忆\n\n" + block + "\n"
        if t.path.exists() and new == t.path.read_text(encoding="utf-8"):
            return Outcome(t.agent, str(t.path), "unchanged", "内容已经一致，没再动文件",
                           chars=f"{len(items)} 条")
        bak = self._backup(t)
        tmp = t.path.with_suffix(t.path.suffix + ".acp-room.tmp")
        tmp.write_text(new, encoding="utf-8", newline="\n")
        os.replace(tmp, t.path)
        return Outcome(t.agent, str(t.path), "written",
                       f"标记块内 {len(items)} 条已更新（块外内容原样保留）",
                       backup=bak, chars=f"{len(new):,} 字节")

    # ----- Hermes store -----

    @staticmethod
    def _hermes_entries(raw: str) -> list[str]:
        return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]

    def _hermes_limit(self, store_path: Path) -> int:
        """从 config.yaml 里抓 memory_char_limit。不引 yaml 依赖，正则够用。"""
        for cand in (store_path.parent.parent / "config.yaml",
                     store_path.parent.parent / "config.yml"):
            if cand.exists():
                try:
                    m = re.search(r"^[\s#]*memory_char_limit:\s*(\d+)",
                                  cand.read_text(encoding="utf-8"), re.M)
                    if m:
                        return int(m.group(1))
                except OSError:
                    pass
        return HERMES_LIMIT_DEFAULT

    def _hermes_usage(self, raw: str, store_path: Path) -> str:
        n = len(ENTRY_DELIMITER.join(self._hermes_entries(raw)))
        return f"{n:,}/{self._hermes_limit(store_path):,}"

    def _write_hermes(self, t: Target, rows: list[dict]) -> Outcome:
        if not t.path.exists():
            return Outcome(t.agent, str(t.path), "skipped",
                           "Hermes 的 MEMORY.md 还不存在，我不替它建（它自己有初始化流程）")
        raw = t.path.read_text(encoding="utf-8")
        entries = self._hermes_entries(raw)
        limit = self._hermes_limit(t.path)

        # 漂移判定，和 Hermes 自己的口径一致：一条比整份预算还长 = 有人塞了自由文本。
        # 这时重写会丢掉那段，Hermes 自己也会拒绝写。我同样不动。
        drift = [e for e in entries if len(e) > limit]
        if drift:
            return Outcome(t.agent, str(t.path), "failed",
                           f"文件里有一条 {len(drift[0]):,} 字的条目超过整份预算 {limit:,}，"
                           "看起来是手写的自由文本。重写会把它弄丢，所以没动 —— "
                           "先按 § 分段那条，再让我写。",
                           chars=self._hermes_usage(raw, t.path))

        others = [e for e in entries if not e.startswith(HERMES_MARK)]
        # 预算按最终 join 结果算（和 Hermes 的 _char_count 同一个算法），
        # 分隔符本身也占字数 —— 忘了这点就会写出刚好超一条的失败。
        base = len(ENTRY_DELIMITER.join(others)) if others else 0
        if base > limit:
            return Outcome(t.agent, str(t.path), "failed",
                           f"你自己的记忆已经用了 {base:,}/{limit:,} 字，超了预算。"
                           "精简归你，不该由我删，所以没动。",
                           chars=self._hermes_usage(raw, t.path))

        mine = [f"{HERMES_MARK} {head}：{txt}" for head, txt in self._lines(rows, 200)]
        while mine:
            joined = ENTRY_DELIMITER.join(others + mine)
            if len(joined) <= limit:
                if joined == raw:
                    return Outcome(t.agent, str(t.path), "unchanged", "内容已经一致，没再动文件",
                                   chars=f"{len(joined):,}/{limit:,}")
                bak = self._backup(t)
                self._atomic_write(t.path, joined)
                return Outcome(t.agent, str(t.path), "written",
                               f"写入 {len(mine)} 条（以 {HERMES_MARK} 标记，"
                               "只替换我自己的老条目）；下次它的会话生效",
                               backup=bak, chars=f"{len(joined):,}/{limit:,}")
            mine.pop(0)   # 先丢最老的那条我们的，仍超就继续丢
        return Outcome(t.agent, str(t.path), "failed",
                       f"预算只剩 {limit - base:,} 字，连一条结论都放不下"
                       f"（你自己的条目占了 {base:,}/{limit:,}）。没动。",
                       chars=f"{base:,}/{limit:,}")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """临时文件 + rename：Hermes 那边可能正在读，不能让读者看到截断的文件。"""
        tmp = path.with_suffix(path.suffix + ".acp-room.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    # ---------- 撤销 ----------

    def retract(self, agents: list[str] | None = None) -> list[dict]:
        """把**我们自己写进去的那部分**收回。

        房间清空时不调用这个，会出现一个很难解释的状态：房间侧的转录和记忆
        都留档作废了，agent 侧却还在"记得"那些结论。收回只碰自己的块 /
        自己的 `[acp-room]` 条目，用户手写的部分一概不动。
        """
        ids = agents if agents is not None else self.agent_ids
        out: list[dict] = []
        for aid in ids:
            t = self.target_for(aid)
            if t.path is None or not t.path.exists():
                out.append(Outcome(aid, str(t.path or ""), "skipped", "没有可收回的内容").as_dict())
                continue
            try:
                raw = t.path.read_text(encoding="utf-8")
                if t.fmt == "hermes":
                    kept = [e for e in self._hermes_entries(raw) if not e.startswith(HERMES_MARK)]
                    if len(kept) == len(self._hermes_entries(raw)):
                        out.append(Outcome(aid, str(t.path), "skipped", "不是我写的，没动").as_dict())
                        continue
                    self._backup(t)
                    self._atomic_write(t.path, ENTRY_DELIMITER.join(kept))
                    out.append(Outcome(aid, str(t.path), "written",
                                       f"收回了自己的条目，留下用户自己的 {len(kept)} 条").as_dict())
                else:
                    if MARK_BEGIN not in raw:
                        out.append(Outcome(aid, str(t.path), "skipped", "不是我写的，没动").as_dict())
                        continue
                    new = re.sub(r"\n*" + re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n*",
                                 "\n", raw, count=1, flags=re.S)
                    self._backup(t)
                    tmp = t.path.with_suffix(t.path.suffix + ".acp-room.tmp")
                    tmp.write_text(new, encoding="utf-8", newline="\n")
                    os.replace(tmp, t.path)
                    out.append(Outcome(aid, str(t.path), "written",
                                       "标记块已收回（块外的用户内容原样保留）").as_dict())
            except Exception as exc:      # noqa: BLE001
                out.append(Outcome(aid, str(t.path), "failed", f"收回异常：{exc}").as_dict())
        return out

    # ---------- 备份 ----------

    def _backup(self, t: Target) -> str:
        if not t.path.exists():
            return ""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = self.dir / f"{t.agent}-{stamp}{t.path.suffix}.bak"
        shutil.copy2(t.path, dst)
        return str(dst)


def summarize(outcomes: list[dict]) -> str:
    """给 announce / 审计用的一行人话。"""
    n: dict[str, int] = {}
    for o in outcomes:
        k = o.get("status", "failed")
        n[k] = n.get(k, 0) + 1
    order = [("written", "写入"), ("unchanged", "未变"), ("skipped", "跳过"), ("failed", "失败")]
    return "、".join(f"{label}{n[k]}" for k, label in order if n.get(k)) or "没有可写的落点"
