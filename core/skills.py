"""技能加载 —— 【P3 归 codex】

Agent Skills 规范（agentskills.io/specification）：一个技能 = 一个目录 + 一个 SKILL.md

    ---
    name: skill-name
    description: 什么时候该用这个技能
    ---
    # 正文：怎么做

**关键机制是动态加载**：不把所有正文塞进 prompt，只列 name + description；
agent 说要用 → 才读正文。否则 40 个技能能把上下文撑爆。

本机现有（实测）：

    ~/.claude/skills/{herdr, hypit, reverse-engineer-anything}
    ~/.pi/agent/skills/{herdr, hypit}
    ~/.codex/skills/{herdr}
    ~/.agents/skills/{hypit, reverse-engineer-anything}

---

## 接口契约（room.py 依赖，签名不许改）

    class Skills:
        Skills(roots=None, enabled=True)
        .scan() -> list[Skill]
        .catalog() -> list[dict]
        .catalog_prompt(max_items=20, desc_limit=100) -> str
        .load(name) -> str | None
        .wants(text) -> list[str]
        .inject(agent_id, names) -> str
        .reset_injection(agent_id=None) -> None
        .broken_count -> int

以上全部保留，P3 只做**新增**，不修改既有行为。

## P3 扩展（按 PROJ/spec/P3-技能加载.md）

1. **同名冲突可配置** `duplicate_policy = first|last|largest|newest|reject`
   （原来写死"先扫到的赢 + log 后忽略"）。冲突双方、策略、胜者都记进 `conflicts()`，
   并带在 `catalog()` 每条 entry 里，前端直接能读。
2. **catalog_prompt 截断** `mode = full|short|none` + `budget_chars` + `max_items` +
   `list_all_names` + `query` 相关度排序。40 个技能时不再硬切前 20 个然后丢信息。
3. **注入队列** `queue_injection()` / `injection_for()`：room.py 的接缝用，
   落盘 turn 时排队、拼 prompt 时取走，一次注入不重复。
4. **每 agent 目录** `per_agent`。
5. **配置自读** 自动读 `acp-room/config.json` 的 `skills` 块，room.py 不用改就能生效。

## 配置（config.json 的 "skills" 块）

    "skills": {
      "enabled": true,
      "roots": ["~/.claude/skills", "~/.agents/skills"],
      "per_agent": {"claude": ["~/.claude/skills"], "codex": ["~/.codex/skills"]},
      "duplicate_policy": "first",
      "conflict_prefer": ["~/.claude/skills"],
      "catalog": {
        "mode": "full", "max_items": 20, "desc_limit": 100,
        "short_desc_limit": 60, "budget_chars": 1200, "list_all_names": true
      },
      "inject": {"max_body_chars": 40000, "once_only": true}
    }

所有键都有默认值，缺了不报错。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from core.gui_log import log

DEFAULT_ROOTS = [
    "~/.claude/skills",
    "~/.agents/skills",
    "~/.pi/agent/skills",
    "~/.codex/skills",
]

USE_RE = re.compile(r"\[USE_SKILL:\s*([A-Za-z0-9_.-]+)\s*\]")
FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
KEYVAL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")

NL = chr(10)   # 模块级换行，inject/_assemble 共用

ROOM_ROOT = Path(__file__).resolve().parent.parent
ROOM_CONFIG = ROOM_ROOT / "config.json"

DUPLICATE_POLICIES = ("first", "last", "largest", "newest", "reject")
PROMPT_MODES = ("full", "short", "none")

_DEFAULT_CONFIG: dict = {
    "enabled": True,
    "roots": None,
    "per_agent": {},
    "duplicate_policy": "first",
    "conflict_prefer": [],
    "catalog": {
        "mode": "full",
        "max_items": 20,
        "desc_limit": 100,
        "short_desc_limit": 60,
        "budget_chars": 1200,
        "list_all_names": True,
    },
    "inject": {"max_body_chars": 40000, "once_only": True},
}


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str
    bytes: int
    conflicts: list = field(default_factory=list)   # 被它挤掉的 Skill 列表
    root_index: int = 0
    mtime: float = 0.0

    def entry(self) -> dict:
        return {"name": self.name, "description": self.description,
                "path": str(self.path), "source": self.source, "bytes": self.bytes,
                # root_index 给前端排序用：不带上的话 JS 只能按名字排，
                # 跟 Python 侧 (root_index, name) 的口径不一致。
                "root_index": self.root_index, "mtime": self.mtime,
                "conflicts": [{"path": str(c.path), "source": c.source,
                               "bytes": c.bytes} for c in self.conflicts]}


def parse_front(text: str) -> dict | None:
    """解析 frontmatter。**解析失败返回 None，绝不抛异常**——技能文件是人手写的。

    支持三种写法：
      - `key: value`            一行，可带引号
      - `key: "value"` / `'v'`  引号包起来（值里可以有冒号）
      - `key: |` / `key: >`      YAML 块标量，后续缩进行拼成一个值
                   （多行 description 很常见，不处理就会被判成坏文件）
    """
    m = FRONT_RE.match((text or "").lstrip("\ufeff"))
    if not m:
        return None
    out: dict = {}
    pend_key: str | None = None
    pend_lines: list[str] = []

    def flush() -> None:
        nonlocal pend_key, pend_lines
        if pend_key is not None:
            out[pend_key] = " ".join(x.strip() for x in pend_lines).strip()
            pend_key, pend_lines = None, []

    for line in m.group(1).splitlines():
        if pend_key is not None:
            if not line.strip():
                continue
            if line[:1].isspace():
                pend_lines.append(line)
                continue
            flush()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kv = KEYVAL_RE.match(line)
        if not kv:
            continue                      # 不规范行：跳过，不炸
        key, val = kv.group(1).lower(), kv.group(2).strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            pend_key, pend_lines = key, []
            continue
        flush()
        val = val.strip().strip('"').strip("'")
        if key in ("name", "description"):
            out[key] = val
    flush()
    if not out.get("name"):
        return None
    return out


def clip(text: str, limit: int) -> str:
    """截断 description，保留单行（换行在 prompt 里会破坏列表排版）。"""
    one = " ".join((text or "").split())
    if limit <= 0:
        return ""
    if len(one) <= limit:
        return one
    return one[:limit].rstrip() + "…"


def tokens(text: str) -> set[str]:
    """相关度排序用的分词。英文按词，中文按字（没有分词器，够用）。"""
    out: set[str] = set()
    for piece in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", text or ""):
        p = piece.lower()
        if not p:
            continue
        out.add(p)
        if re.search(r"[\u4e00-\u9fff]", p):
            out.update(re.findall(r"[\u4e00-\u9fff]", p))
    return out

class Skills:
    """扫描 + 清单 + 按需注入。Room 持有一个。"""

    def __init__(self, roots: list[str | Path] | None = None, enabled: bool = True,
                 config: dict | None = None) -> None:
        self.cfg: dict = json.loads(json.dumps(_DEFAULT_CONFIG))   # 深拷贝默认值
        self.load_config(config)                                   # 合并 room config.json
        self.enabled = bool(self.cfg.get("enabled", True)) if enabled is None else enabled
        self.roots = self._resolve_roots(roots or self.cfg.get("roots") or DEFAULT_ROOTS)
        self._skills: dict[str, Skill] = {}
        self._scanned: list[dict] = []
        self._conflicts: list[dict] = []
        self._root_index: dict[str, int] = {}
        self._broken = 0
        self.policy = str(self.cfg.get("duplicate_policy", "first"))
        self._pending: dict[str, str] = {}          # agent_id -> 待注入块
        self._injected: set[tuple[str, str]] = set()   # (agent_id, skill_name) 已注入过
        self.scan()

    # ---------- 配置 ----------

    def load_config(self, config: dict | None = None) -> None:
        """合并配置：显式传入 > acp-room/config.json 的 skills 块 > 默认值。

        读失败**必须说出来**。原来 except 里什么都不写，静默回退默认 roots，
        于是 config.json 里的技能目录不生效却毫无痕迹。实测踩过两次：
          1. "./skills" 相对路径按进程 cwd 解析，指向 workspace 而不是 acp-room/；
          2. config.json 被写成带 BOM 的 UTF-8，json.loads 抛 ValueError。
        两种都表现为"技能少了几个，其他一切正常"，是最难查的失败方式。
        """
        merged: dict = json.loads(json.dumps(_DEFAULT_CONFIG))
        room_cfg: dict = {}
        try:
            room_cfg = json.loads(ROOM_CONFIG.read_text(encoding="utf-8-sig")).get("skills") or {}
        except FileNotFoundError:
            room_cfg = {}
        except (OSError, ValueError) as exc:
            room_cfg = {}
            log(f"[skills] WARN 读 {ROOM_CONFIG} 失败（{type(exc).__name__}: {exc}），"
                f"skills 配置回退默认值——config.json 里的 skills 块本次不生效")
        for src in (room_cfg, config or {}):
            if not isinstance(src, dict):
                continue
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        self.cfg = merged

    def configure(self, config: dict | None = None) -> dict:
        """运行期改配置（room.py 若需要，调这个，不用重建 Skills）。"""
        self.load_config(config)
        self.enabled = bool(self.cfg.get("enabled", True))
        self.roots = self._resolve_roots(self.cfg.get("roots") or DEFAULT_ROOTS)
        self._root_index = {str(r): i for i, r in enumerate(self.roots)}
        self.scan()
        self.reset_injection()
        return self.effective_config()

    def effective_config(self) -> dict:
        """当前生效的配置（机器可读，给前端/CLI）。"""
        return {
            "enabled": self.enabled,
            "roots": [str(r) for r in self.roots],
            "per_agent": self.cfg.get("per_agent") or {},
            "duplicate_policy": self.cfg.get("duplicate_policy", "first"),
            "conflict_prefer": list(self.cfg.get("conflict_prefer") or []),
            "catalog": dict(self.cfg.get("catalog") or {}),
            "inject": dict(self.cfg.get("inject") or {}),
        }

    @staticmethod
    def _resolve_roots(raw) -> list[Path]:
        """相对路径按 ROOM_ROOT（acp-room/）解析。

        加这一条是因为 config.json 里写 `"./skills"` 时，原来会按**进程 cwd** 解析，
        而 room 的 cwd 是 workspace（Desktop/交流），于是 acp-room/skills 下的技能
        一个都扫不到，而且不报错——静默失效是这里最坏的失败方式。
        `~` 仍按用户主目录展开；绝对路径原样使用。
        """
        out = []
        for r in raw or []:
            s = str(r)
            if s.startswith("~"):
                p = Path(s).expanduser()
            else:
                p = Path(s)
                if not p.is_absolute():
                    p = ROOM_ROOT / p
                p = Path(os.path.normpath(p))
            if p not in out:
                out.append(p)
        return out

    def _ordered_roots(self) -> list[Path]:
        """conflict_prefer 里的目录提到最前——这就是"就近优先"的可配置表达：
        声明顺序 = 优先级，把最可信的目录写在最前面即可。"""
        prefer = [Path(str(r)).expanduser() for r in (self.cfg.get("conflict_prefer") or [])]
        first = [r for r in prefer if r in self.roots]
        rest = [r for r in self.roots if r not in first]
        return first + rest

    def roots_for(self, agent_id: str | None = None) -> list[Path]:
        if agent_id is None:
            return self.roots
        per = (self.cfg.get("per_agent") or {}).get(agent_id)
        if not per:
            return self.roots
        out = self._resolve_roots(per)
        return out or self.roots

    # ---------- 扫描 ----------

    def scan(self) -> list[Skill]:
        self._skills.clear()
        self._scanned.clear()
        self._conflicts.clear()
        self._root_index = {str(r): i for i, r in enumerate(self._ordered_roots())}
        self._broken = 0
        policy = str(self.cfg.get("duplicate_policy", "first"))
        if policy not in DUPLICATE_POLICIES:
            log(f"[skills] 未知 duplicate_policy={policy}，回退 first")
            policy = "first"
        self.policy = policy

        for root in self._ordered_roots():
            if not root.is_dir():
                continue
            # 深度 1 + 深度 2：~/.codex/skills/.system/<name>/SKILL.md 这种分组布局
            # 也是合法技能根；同根内同名冲突时 shallower 赢（深度 1 先扫）。
            for md in (sorted(root.glob("*/SKILL.md"))
                       + sorted(root.glob("*/*/SKILL.md"))):
                try:
                    text = md.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    log(f"[skills] 读不到 {md}: {exc}")
                    self._broken += 1
                    self._scanned.append({"path": md, "source": str(root), "status": "broken",
                                          "reason": str(exc)})
                    continue
                meta = parse_front(text)
                if meta is None:
                    # 可能是 .md 但没 frontmatter（比如 spec 文件），只要在 skills/ 下的子目录里就算坏
                    self._broken += 1
                    self._scanned.append({"path": md, "source": str(root), "status": "broken",
                                          "reason": "frontmatter 解析失败或缺 name"})
                    continue
                name = meta["name"]
                st = Skill(
                    name=name,
                    description=meta.get("description", ""),
                    path=md,
                    source=str(root),
                    bytes=md.stat().st_size,
                    root_index=self._root_index.get(str(root), 999),
                    mtime=md.stat().st_mtime,
                )
                old = self._skills.get(name)
                if old is None:
                    self._skills[name] = st
                    self._scanned.append({"path": md, "source": str(root), "status": "ok",
                                          "skill": st})
                    continue
                if policy == "reject":
                    self._broken += 2
                    self._scanned.append({"path": md, "source": str(root), "status": "rejected",
                                          "skill": st, "reason": f"policy=reject 丢弃 {name}"})
                    del self._skills[name]
                    self._scanned[-2]["status"] = "rejected"
                    self._scanned[-2]["reason"] = f"policy=reject 丢弃 {name}"
                    self._conflicts.append({"name": name, "policy": policy,
                                            "kept": None,
                                            "dropped": [{"path": str(old.path),
                                                         "source": old.source,
                                                         "bytes": old.bytes},
                                                        {"path": str(st.path),
                                                         "source": st.source,
                                                         "bytes": st.bytes}]})
                    continue
                winner, loser = self._winner(old, st, policy)
                if winner is old:
                    old.conflicts.append(loser)
                    self._scanned.append({"path": md, "source": str(root), "status": "duplicate",
                                          "skill": st, "kept": str(old.path), "kept_name": old.name,
                                          "kept_source": old.source})
                else:
                    st.conflicts.append(old)
                    self._skills[name] = st
                    self._scanned.append({"path": md, "source": str(root), "status": "ok",
                                          "skill": st})
                    self._scanned[-2]["status"] = "duplicate"
                    self._scanned[-2]["kept"] = str(st.path)
                    self._scanned[-2]["kept_source"] = st.source
                self._conflicts.append({"name": name, "policy": policy,
                                        "kept": str(self._skills[name].path),
                                        "kept_source": self._skills[name].source,
                                        "dropped": [{"path": str(c.path),
                                                     "source": c.source,
                                                     "bytes": c.bytes} for c in
                                                   self._skills[name].conflicts]})
                log(f"[skills] 同名冲突 {name}: policy={policy} 用 {self._skills[name].path}")
        log(f"[skills] 扫描完成：{len(self._skills)} 个可用，{self._broken} 个解析失败，"
            f"{len(self._conflicts)} 组冲突")
        return list(self._skills.values())

    @staticmethod
    def _winner(old: Skill, new: Skill, policy: str) -> tuple[Skill, Skill]:
        """按 policy 决定同名冲突谁赢，返回 (winner, loser)。"""
        if policy == "last":
            return new, old
        if policy == "largest":
            return (new, old) if new.bytes >= old.bytes else (old, new)
        if policy == "newest":
            return (new, old) if new.mtime >= old.mtime else (old, new)
        return old, new                      # first

    def _index_for(self, agent_id: str | None = None) -> dict[str, Skill]:
        """按 agent 的目录范围重新解一次同名冲突。

        **这一步是必须的**：全局 _skills 是跨所有 roots 去重的，如果某个 agent
        只配了靠后的根目录，它的技能会被前面的根目录挤光（实测 per_agent 配单个
        根目录时 catalog 返回 0 个）。冲突要在 agent 自己的目录范围内解决。
        """
        if agent_id is None:
            return dict(self._skills)
        allowed = {str(r) for r in self.roots_for(agent_id)}
        out: dict[str, Skill] = {}
        for e in self._scanned:
            if e.get("status") == "broken" or "skill" not in e:
                continue
            if e["source"] not in allowed:
                continue
            s = e["skill"]
            old = out.get(s.name)
            if old is None:
                out[s.name] = s
                continue
            win, lose = self._winner(old, s, self.policy)
            if win is old:
                old.conflicts = [c for c in old.conflicts if c is not lose]
                old.conflicts.append(lose)
            else:
                win.conflicts = [c for c in win.conflicts if c is not lose]
                win.conflicts.append(lose)
                out[s.name] = win
        return out

    def catalog(self, agent_id: str | None = None) -> list[dict]:
        """生效目录（同名已去重）。传 agent_id 则按 per_agent 的目录过滤。

        不传 agent_id 时用全局索引（向后兼容，room.py 现有调用不变）。
        """
        if agent_id is None:
            return [s.entry() for s in self._skills.values()]
        return [s.entry() for s in self._index_for(agent_id).values()]

    def entries_raw(self) -> list[dict]:
        """原始条目：每个 SKILL.md 一条，带 ok/duplicate/broken/rejected 状态。"""
        out = []
        for e in self._scanned:
            row = {"path": str(e["path"]), "source": e["source"], "status": e["status"]}
            for kk in ("kept", "kept_source", "kept_name", "reason"):
                if kk in e:
                    row[kk] = e[kk]
            if "reason" in e:
                row["reason"] = e["reason"]
            if "skill" in e:
                row["name"] = e["skill"].name
                row["description"] = e["skill"].description
                row["bytes"] = e["skill"].bytes
            out.append(row)
        return out

    @property
    def broken_count(self) -> int:
        return self._broken

    def conflicts(self) -> list[dict]:
        return list(self._conflicts)

    # ---------- 清单注入 ----------

    def _ranked(self, agent_id: str | None, query: str) -> list[Skill]:
        items = list(self._index_for(agent_id).values())
        q = tokens(query)
        if q:
            def score(s: Skill) -> tuple:
                hit_name = len(tokens(s.name) & q)
                hit_desc = len(tokens(s.description) & q)
                return (-(3 * hit_name + hit_desc), s.root_index, s.name)
            return sorted(items, key=score)
        return sorted(items, key=lambda s: (s.root_index, s.name))

    def _drop_line(self, dropped: list[Skill], list_all_names: bool) -> list[str]:
        if not dropped:
            return []
        if list_all_names:
            names = ", ".join(s.name for s in dropped)
            return [f"其余 {len(dropped)} 个（只给名字）：{names}"]
        return [f"（还有 {len(dropped)} 个未列出，可用 [USE_SKILL: 名字] 点名）"]

    def _assemble(self, items, header, str_footer, max_items, limit, budget,
                  list_all_names):
        """拼清单，budget 是**总长**上限。

        下界：就算砍光所有 description，「其余 N 个（只给名字）」那行也得把名字列全
        （清单是发现机制，注入只认 [USE_SKILL: 名字]）。预算低于这个下界时
        只能照发并告警——不可能更短。
        """
        NL = chr(10)
        footer = str_footer

        def line_for(s):
            return (f"- {s.name}" if limit == 0
                    else f"- {s.name}: {clip(s.description, limit)}")

        names = ", ".join(s.name for s in items)
        drop_line = (f"其余 {len(items)} 个（只给名字）：{names}"
                     if list_all_names else
                     f"（还有 {len(items)} 个未列出，可用 [USE_SKILL: 名字] 点名）")
        floor = NL.join([header, drop_line, footer]) if items else NL.join([header, footer])
        if budget > 0 and len(floor) > budget:
            log(f"[skills] 预算 {budget} 字符低于「全列名字」下界 {len(floor)}，只能照发")
            return floor

        base = len(header) + len(footer) + 1
        # 预算给「名字名单」那行留满，剩下的才分给 description
        reserve = base + len(drop_line)
        avail = (budget - reserve) if budget > 0 else None
        used, shown, dropped = 0, [], []
        for s in items:
            ln = line_for(s)
            cost = len(ln) + 1
            if len(shown) >= max_items or (avail is not None and used + cost > avail):
                dropped.append(s)
                continue
            shown.append(ln)
            used += cost
        real_drop = self._drop_line(dropped, list_all_names)
        return NL.join([header] + shown + real_drop + [footer])
    def catalog_prompt(self, max_items: int = 20, desc_limit: int = 100,
                       query: str = "", agent_id: str | None = None,
                       mode: str | None = None, budget_chars: int | None = None,
                       list_all_names: bool | None = None) -> str:
        """生成清单文本。要短——这一段每轮都会进 prompt。

        截断策略（40 个技能时）：
          - `mode=full`    name + description（截到 desc_limit）
          - `mode=short`   name + description 前 short_desc_limit 字
          - `mode=none`    只列 name，一个 description 都不给
          - `budget_chars` 总字符预算（含头尾与"其余 N 个"那行），超了就砍
          - `list_all_names` 被砍掉的仍以紧凑名单列出 —— **关键**：清单是发现机制，
            砍掉 description 可以，砍掉名字就废了，因为注入只认 `[USE_SKILL: 名字]`
          - `query`        相关度排序（命中 name 权重 3，命中 description 权重 1）
        """
        if not self.enabled or not self._skills:
            return ""
        ccfg = self.cfg.get("catalog") or {}
        mode = (mode or ccfg.get("mode") or "full").lower()
        if mode not in PROMPT_MODES:
            mode = "full"
        short_limit = int(ccfg.get("short_desc_limit", 60))
        limit = desc_limit if mode == "full" else (short_limit if mode == "short" else 0)
        budget = int(budget_chars if budget_chars is not None
                     else ccfg.get("budget_chars", 1200))
        if list_all_names is None:
            list_all_names = bool(ccfg.get("list_all_names", True))

        items = self._ranked(agent_id, query)
        header = f"[可用技能 · 共 {len(items)} 个]"
        footer = "要用某个技能，在你回复里写 [USE_SKILL: 名字]，我会把正文给你。"
        return self._assemble(items, header, footer, max_items, limit, budget,
                              list_all_names)
    # ---------- 按需注入 ----------

    def load(self, name: str, agent_id: str | None = None) -> str | None:
        s = (self._index_for(agent_id).get(name) if agent_id
             else self._skills.get(name))
        if not s:
            return None
        try:
            return s.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def wants(self, text: str, agent_id: str | None = None) -> list[str]:
        """agent 这条回复里点名要哪些技能。传 agent_id 时只认它目录范围内的技能。"""
        pool = self._index_for(agent_id) if agent_id else self._skills
        return [m for m in USE_RE.findall(text or "") if m in pool]

    def _cap(self, text: str) -> str:
        cap = int((self.cfg.get("inject") or {}).get("max_body_chars", 0) or 0)
        if cap > 0 and len(text) > cap:
            return text[:cap] + f"\n…（正文 {len(text)} 字符，已截断到 {cap}）"
        return text

    def inject(self, agent_id: str, names: list[str]) -> str:
        """把正文拼成要附在下一轮 prompt 前面的块。

        同一个 agent 同一个技能只注入一次（once_only）。正文取自该 agent 目录范围内的
        那份 SKILL.md，而不是全局赢家。
        """
        pool = self._index_for(agent_id)
        blocks = []
        for n in names:
            if (agent_id, n) in self._injected:
                continue
            s = pool.get(n)
            if s is None:
                continue
            try:
                body = s.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._injected.add((agent_id, n))
            blocks.append(f"=== 技能 {n} 正文 ==={NL}{self._cap(body)}{NL}=== 结束 ===")
        return NL.join(blocks) if blocks else ""

    def queue_injection(self, agent_id: str, names: list[str]) -> str:
        """room.py 接缝：落盘一条 turn 之后调用，把本轮点名的技能正文排进该 agent 的队列。

        返回本次**新增**的块（空串 = 没有新内容）。已在队列里或已注入过的不会重复。
        """
        block = self.inject(agent_id, names)
        if not block:
            return ""
        prev = self._pending.get(agent_id, "")
        self._pending[agent_id] = (prev + "\n\n" + block).strip() if prev else block
        log(f"[skills] {agent_id} 排队注入 {len(block)} 字符（累计 "
            f"{len(self._pending[agent_id])}）")
        return block

    def injection_for(self, agent_id: str) -> str:
        """room.py 接缝：拼 prompt 时调用，取走该 agent 的待注入块（取走即清空）。"""
        return self._pending.pop(agent_id, "")

    def pending_for(self, agent_id: str) -> str:
        """只看不取。给前端/调试用。"""
        return self._pending.get(agent_id, "")

    def reset_injection(self, agent_id: str | None = None) -> None:
        if agent_id is None:
            self._injected.clear()
            self._pending.clear()
        else:
            self._injected = {k for k in self._injected if k[0] != agent_id}
            self._pending.pop(agent_id, None)

def _fmt_bytes(n: int) -> str:
    return f"{n}B" if n < 1024 else f"{n/1024:.1f}KB"


def _short(path) -> str:
    """C:\\Users\\X\\.claude\\skills -> ~/.claude/skills"""
    try:
        return "~/" + str(Path(str(path)).expanduser().relative_to(Path.home())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _print_catalog(sk: Skills, raw: bool, as_json: bool) -> None:
    rows = sk.entries_raw() if raw else sk.catalog()
    counts = {
        "raw_entries": len(sk.entries_raw()),
        "unique": len(sk.catalog()),
        "broken": sk.broken_count,
        "conflicts": len(sk.conflicts()),
        "with_description": sum(1 for r in rows
                               if r.get("status") != "broken" and r.get("description")),
    }
    if as_json:
        print(json.dumps({"counts": counts, "roots": [str(r) for r in sk.roots],
                          "entries": rows if raw else sk.catalog(),
                          "conflicts": sk.conflicts(),
                          "config": sk.effective_config()},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"可用技能 {counts['unique']} 个（原始条目 {counts['raw_entries']} 个，"
          f"冲突 {counts['conflicts']} 组，解析失败 {counts['broken']} 个）")
    for r in rows:
        if raw and r.get("status") == "broken":
            print(f"  BRK {Path(r['path']).parent.name:29} {_fmt_bytes(0):>8}  "
                  f"{_short(r['source']):24}  broken: {str(r.get('reason', ''))[:40]}")
            continue
        mark = "OK " if not raw else {"ok": "OK ", "duplicate": "DUP",
                                "broken": "BRK", "rejected": "REJ"}.get(
            r.get("status"), "?  ")
        star = ""
        if r.get("status") == "duplicate":
            kept = r.get("kept") or ""
            if kept:
                star = "  [conflict -> 赢: " + _short(r.get("kept_source")
                                          or Path(kept).parent.parent) + "]"
            else:
                star = "  [conflict]"
        elif r.get("status") == "broken" and raw:
            star = "  [" + str(r.get("reason", ""))[:40] + "]"
        print(f"  {mark} {r['name']:30} {_fmt_bytes(r.get('bytes', 0)):>8}  "
              f"{_short(r['source']):24} {clip(r.get('description', ''), 60)}{star}")
    missing = [r for r in rows if r.get("status") != "broken" and not r.get("description")]
    if missing:
        print(f"  warn: 缺 description {missing}")


def _print_prompt(sk: Skills, args) -> None:
    text = sk.catalog_prompt(
        max_items=args.max_items,
        desc_limit=args.desc_limit,
        query=args.query or "",
        agent_id=args.per_agent,
        mode=args.mode,
        budget_chars=args.budget,
        list_all_names=(None if args.list_names is None else args.list_names),
    )
    if args.json:
        print(json.dumps({"prompt": text, "chars": len(text),
                          "mode": args.mode or (sk.cfg.get("catalog") or {}).get("mode"),
                          "max_items": args.max_items,
                          "desc_limit": args.desc_limit}, ensure_ascii=False, indent=2))
    else:
        print(text)
        print(f"\n[prompt 字符数 {len(text)}]")


def _simulate(sk: Skills, text: str, as_json: bool) -> None:
    """模拟一轮：打印清单 -> 检测点名 -> 注入 -> 再调一次证明不重复。"""
    q = text or ""
    names = sk.wants(q)
    if not names:
        auto = [s.name for s in sk._skills.values()
                if re.search(r"\b" + re.escape(s.name) + r"\b", q)]
        names = auto or ([next(iter(sk._skills))] if sk._skills else [])
        q = q + " [USE_SKILL: " + (names[0] if names else "") + "]"
    out: dict = {"catalog_prompt": sk.catalog_prompt(query=q),
                 "detected": names, "queued_chars": 0, "requeued_chars": 0,
                 "injected_preview": ""}

    block = sk.queue_injection("demo", names)
    out["queued_chars"] = len(block)
    out["pending_chars"] = len(sk.pending_for("demo"))
    picked = sk.injection_for("demo")
    out["picked_chars"] = len(picked)
    out["pending_after_pick"] = len(sk.pending_for("demo"))
    out["injected_preview"] = picked[:300]

    second = sk.queue_injection("demo", names)
    out["requeued_chars"] = len(second)
    out["repeat_injected"] = bool(second)

    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    print("--- 清单（每轮进 prompt 的部分）---")
    print(out["catalog_prompt"])
    print(f"\n--- 模拟 agent 回复要点技能 ---")
    print(f"检测到：{names}")
    print(f"queue_injection -> {out['queued_chars']} 字符；pending {out['pending_chars']} 字符")
    print(f"injection_for  -> 取走 {out['picked_chars']} 字符；取走后 pending "
          f"{out['pending_after_pick']} 字符")
    print("\n--- 注入内容（前 300 字符）---")
    print(out["injected_preview"])
    print(f"\n第二次 queue_injection -> {out['requeued_chars']} 字符"
          + ("（已注入过，跳过 ✅ 只注入一次）" if not second else "（❌ 重复注入）"))


def _force_utf8_stdio() -> None:
    """Windows console defaults to GBK; special glyphs raise UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="")


def _main() -> None:
    _force_utf8_stdio()
    import argparse
    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    ap = argparse.ArgumentParser(description="P3 技能加载：扫描 / 清单 / 注入")
    ap.add_argument("--catalog", action="store_true",
                    help="列出技能（默认含重复条目与冲突标记）")
    ap.add_argument("--effective", action="store_true",
                    help="只列生效目录（同名已去重）")
    ap.add_argument("--include-broken-stats", action="store_true",
                    help="附解析失败计数")
    ap.add_argument("--conflicts", action="store_true", help="打印冲突明细")
    ap.add_argument("--dump-config", action="store_true", help="打印生效配置（JSON）")
    ap.add_argument("--prompt", action="store_true", help="打印 catalog_prompt")
    ap.add_argument("--query", default="", help="--prompt 的相关度查询词")
    ap.add_argument("--mode", choices=list(PROMPT_MODES), default=None,
                    help="--prompt 的截断模式 full|short|none")
    ap.add_argument("--max-items", type=int, default=None)
    ap.add_argument("--desc-limit", type=int, default=None)
    ap.add_argument("--budget", type=int, default=None, help="--prompt 的字符预算")
    ap.add_argument("--list-names", dest="list_names", action="store_true", default=None,
                    help="--prompt：被砍掉的仍列名字")
    ap.add_argument("--no-list-names", dest="list_names", action="store_false",
                    help="--prompt：被砍掉的不列名字")
    ap.add_argument("--simulate", metavar="TEXT", help="模拟 agent 请求技能并注入")
    ap.add_argument("--per-agent", default=None, help="按 per_agent 目录过滤")
    ap.add_argument("--root", action="append", default=None,
                    help="临时覆盖 roots，可重复")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    sk = Skills(roots=args.root, enabled=True)

    if args.dump_config:
        print(json.dumps(sk.effective_config(), ensure_ascii=False, indent=2, sort_keys=True))
        sys.exit(0)

    if args.max_items is None:
        args.max_items = int((sk.cfg.get("catalog") or {}).get("max_items", 20))
    if args.desc_limit is None:
        args.desc_limit = int((sk.cfg.get("catalog") or {}).get("desc_limit", 100))
    if args.budget is None:
        args.budget = int((sk.cfg.get("catalog") or {}).get("budget_chars", 1200))

    if args.catalog or args.effective:
        _print_catalog(sk, raw=not args.effective, as_json=args.json)
        if args.include_broken_stats:
            print(f"解析失败 {sk.broken_count} 个"
                  + ("（退出码 0，坏文件不炸）" if args.json is False else ""))
        sys.exit(0)

    if args.conflicts:
        if args.json:
            print(json.dumps(sk.conflicts(), ensure_ascii=False, indent=2))
        else:
            print(f"冲突 {len(sk.conflicts())} 组（policy={sk.cfg.get('duplicate_policy')}）")
            for c in sk.conflicts():
                print(f"  {c['name']}: 保留 {c['kept']}")
                for d in c["dropped"]:
                    print(f"      丢弃 {d['path']}（{d['bytes']}B）")
        sys.exit(0)

    if args.prompt:
        _print_prompt(sk, args)
        sys.exit(0)

    if args.simulate:
        _simulate(sk, args.simulate, args.json)
        sys.exit(0)

    ap.print_help()


if __name__ == "__main__":
    _main()