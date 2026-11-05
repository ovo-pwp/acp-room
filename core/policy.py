r"""策略闸门 —— 决定一次工具调用/权限请求是放行、拒绝还是升级给人。
## 为什么需要它

P10 实测（`PROJ/inbox/0013-codex-P10.md`）的结论是：
`config.json` 的 `permission` 字段只有 `allow` / `deny` 两个值真实生效，
`ask` 从来没生效过；而 `audit.jsonl` 里 `decision` 恒定是 `auto_allow`，
`rule` 恒定是 `config.agents[].permission == allow`——**那不是策略，那是常量**。

这个模块把"常量"换成"可判定的规则表"，判定顺序抄 `vendor/acpx/src/permission-policy.ts`：

    autoDeny → autoApprove → escalate → defaultAction → 权限模式兜底

顺序不能换。deny 必须最先，否则一条 allow 就能把 deny 盖掉。

## 三条铁律（照抄 OpenBot `docs/architecture.md:69-73`）

1. **fail-closed**：策略缺失、规则写坏、匹配不上，一律不自动放行。
   坏掉的规则当成"命中"，而不是"忽略"——写错的安全规则应该变严，不是变松。
2. **拒绝要说出理由**：`Decision.rule` 必须指出是哪条规则、匹配了什么。
   拒绝却不解释，等于让人去猜。
3. **审计先行**：调用方拿到 Decision 后**先写 audit 再动手**。
   本模块不写审计，只做判定——判定和执行之间不能有副作用。

## 规则怎么匹配

一条规则是一个 dict，所有给出的字段都必须命中（AND）；未给出的字段不参与。

    {"tool": "execute", "title_contains": "rm -rf"}
    {"path_prefix": "C:/Windows"}
    {"command_regex": r"\bdel\s+/[sq]"}
    {"tool": "read"}
    {}                      # 无条件命中

匹配是**大小写不敏感**的（acpx 的约定）。字段名写错会让规则变严（fail-closed），
所以 `validate()` 会列出未知字段，配置阶段就该报出来。

## 配置来源（config.json，全部可选）

    "policy": {
      "auto_deny":    [ ... ],
      "auto_approve": [ ... ],
      "escalate":     [ ... ],
      "default_action": "deny",
      "enabled": true
    }

`enabled: false` 是唯一的"不过闸"开关，用来做对照实测，审计里会记下这一点。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from core.gui_log import log

DecisionKind = Literal["allow", "deny", "escalate"]

# 规则里允许出现的字段。写错字段名 = 规则永不命中，会造成"以为拦住了其实没拦"，
# 所以这里做白名单，配置阶段就能报出来。
KNOWN_KEYS = {
    "tool", "title", "title_contains", "title_regex",
    "command", "command_contains", "command_regex",
    "path", "path_prefix", "path_contains", "path_suffix",
    "agent", "initiator",
}

# 触发"这条规则被匹配到"的字段越多，说明它越具体。上报时按 specificity 排序，
# 让 operation 看到的理由是人写的那条，而不是兜底的那条。
_MATCH_ORDER = (
    "tool", "agent", "initiator",
    "title", "title_contains", "title_regex",
    "command", "command_contains", "command_regex",
    "path", "path_prefix", "path_contains", "path_suffix",
)


@dataclass
class Subject:
    """被判定的一次动作。

    `tool` / `title` / `command` / `path` 都来自 ACP 的 `session/request_permission`
    或 `session/update` 的工具调用通知，平台自己不再加工——加过工就说不清是谁说的了。
    """

    tool: str = ""
    title: str = ""
    command: str = ""
    path: str = ""
    agent: str = ""
    initiator: str = "person"

    @classmethod
    def from_permission(cls, params: dict, agent: str, initiator: str = "person") -> "Subject":
        """从 ACP `session/request_permission` 的 params 建 Subject。

        ACP 的形状（实测 codex/claude/omp 三家一致）：
            {"toolCall": {"title": "...", "kind": "...", "rawInput": {...}},
             "options": [...]}
        `rawInput` 里可能带 `command` / `path`，有就取，没有就空串。
        """
        tc = params.get("toolCall") or {}
        raw = tc.get("rawInput") or {}
        if not isinstance(raw, dict):
            raw = {}
        cmd = raw.get("command") or raw.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        path = raw.get("path") or raw.get("file_path") or raw.get("target") or ""
        return cls(
            tool=str(tc.get("kind") or raw.get("tool") or ""),
            title=str(tc.get("title") or ""),
            command=str(cmd),
            path=str(path),
            agent=str(agent),
            initiator=str(initiator or "person"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "tool": self.tool, "title": self.title, "command": self.command,
            "path": self.path, "agent": self.agent, "initiator": self.initiator,
        }


@dataclass
class Decision:
    kind: DecisionKind
    rule: str                  # 人话理由：哪条规则、命中什么
    rule_index: int | None = None
    rule_source: str = ""      # auto_deny / auto_approve / escalate / default_action / mode / disabled
    matched: dict[str, str] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "rule": self.rule, "rule_index": self.rule_index,
            "rule_source": self.rule_source, "matched": self.matched,
        }


class Policy:
    """规则表 + 判定。一个 Room 持有一个。

    与 `core.config_opts.py` / `core.skills.py` 一致：自己读 config.json 的 `policy` 块，
    room.py 不用改就能生效。
    """

    ROOM_ROOT = Path(__file__).resolve().parent.parent
    ROOM_CONFIG = ROOM_ROOT / "config.json"

    def __init__(self, config: dict | None = None, enabled: bool | None = None,
                 *, read_disk: bool = True) -> None:
        self.auto_deny: list[dict] = []
        self.auto_approve: list[dict] = []
        self.escalate: list[dict] = []
        self.default_action: DecisionKind = "deny"
        self.enabled: bool = True
        self.warnings: list[str] = []      # 配置阶段的坏规则，启动时该打印出来
        self.configure(config, enabled, read_disk=read_disk)
        self.hits: dict[str, int] = {"allow": 0, "deny": 0, "escalate": 0}

    # ---------- 配置 ----------

    @classmethod
    def from_rules(cls, config: dict | None = None, enabled: bool | None = None) -> "Policy":
        """**不读 config.json**，只用传进来的规则建一个策略。

        单测必须用它。原因是 `configure()` 的 config 参数是**叠加**在磁盘配置之上的
        （`raw = _read_room_config(); raw.update(config)`）——直接 `Policy(config={})`
        会悄悄把线上 config.json 的规则也带进来，于是"没规则就该拒"的用例
        会因为线上配了 default_action=escalate 而失败，而且报错信息完全指不到真因。
        """
        return cls(config, enabled, read_disk=False)

    def configure(self, config: dict | None = None, enabled: bool | None = None,
                  *, read_disk: bool = True) -> None:
        raw = self._read_room_config() if read_disk else {}
        if config:
            raw.update(config)
        self.warnings = []

        self.auto_deny = self._rules(raw.get("auto_deny"), "auto_deny")
        self.auto_approve = self._rules(raw.get("auto_approve"), "auto_approve")
        self.escalate = self._rules(raw.get("escalate"), "escalate")

        da = str(raw.get("default_action") or "deny").strip().lower()
        if da not in ("allow", "deny", "escalate"):
            self.warnings.append(f"default_action={da!r} 不是 allow/deny/escalate，按 deny 处理")
            da = "deny"
        self.default_action = da  # type: ignore[assignment]

        self.enabled = True if enabled is None else bool(enabled)
        if "enabled" in raw and enabled is None:
            self.enabled = bool(raw.get("enabled"))

        for w in self.warnings:
            log(f"[policy] WARN {w}")

    def _read_room_config(self) -> dict:
        """读 config.json 的 policy 块。读失败要说出来——静默回退会让人以为闸门在跑。"""
        try:
            # utf-8-sig：config.json 曾被 PowerShell 写成带 BOM，json.loads 会直接抛。
            data = json.loads(self.ROOM_CONFIG.read_text(encoding="utf-8-sig"))
            return dict(data.get("policy") or {})
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log(f"[policy] WARN 读 {self.ROOM_CONFIG} 失败（{type(exc).__name__}: {exc}），"
                f"policy 配置回退默认值（无条件 deny）")
            return {}

    def _rules(self, items: Any, source: str) -> list[dict]:
        """校验规则。坏规则直接丢弃并记 warning——**不要**让坏规则"匹配一切"，
        那样一个手滑就会让整类动作全被拒，比不加载更难查。"""
        out: list[dict] = []
        if not items:
            return out
        if not isinstance(items, list):
            self.warnings.append(f"{source} 不是数组，已忽略")
            return out
        for i, rule in enumerate(items):
            if not isinstance(rule, dict):
                self.warnings.append(f"{source}[{i}] 不是对象，已忽略")
                continue
            unknown = set(rule) - KNOWN_KEYS
            if unknown:
                self.warnings.append(f"{source}[{i}] 有未知字段 {sorted(unknown)}，这条规则永不命中")
                continue
            bad = self._compile_rule(rule)
            if bad:
                self.warnings.append(f"{source}[{i}] {bad}，已忽略")
                continue
            out.append(rule)
        return out

    @staticmethod
    def _compile_rule(rule: dict) -> str:
        """预编译正则，返回错误说明（空串表示没问题）。"""
        for key in ("title_regex", "command_regex"):
            if key in rule:
                try:
                    re.compile(str(rule[key]), re.IGNORECASE)
                except re.error as exc:
                    return f"{key} 正则无效（{exc}）"
        return ""

    # ---------- 匹配 ----------

    @staticmethod
    def _matches(rule: dict, subj: Subject) -> bool:
        d = subj.as_dict()
        for key, want in rule.items():
            want_s = str(want)
            if key in ("title_regex", "command_regex"):
                field_name = key[:-len("_regex")]
                if not re.search(want_s, d.get(field_name, ""), re.IGNORECASE):
                    return False
            elif key.endswith("_contains") or key.endswith("_prefix") or key.endswith("_suffix"):
                base, op = key.rsplit("_", 1)
                val = d.get(base, "").lower()
                w = want_s.lower()
                if op == "contains" and w not in val:
                    return False
                if op == "prefix" and not val.startswith(w):
                    return False
                if op == "suffix" and not val.endswith(w):
                    return False
            else:
                if d.get(key, "").lower() != want_s.lower():
                    return False
        return True

    def _first_hit(self, rules: list[dict], subj: Subject, source: str) -> tuple[dict, int] | None:
        for i, rule in enumerate(rules):
            if self._matches(rule, subj):
                return rule, i
        return None

    def _describe(self, rule: dict, subj: Subject, source: str, index: int) -> str:
        """人话理由。格式：来源[序号]: 字段=值（命中项）"""
        parts = []
        for key in _MATCH_ORDER:
            if key in rule:
                if key in ("title_regex", "command_regex"):
                    parts.append(f"{key}={rule[key]!r}")
                else:
                    base = key.rsplit("_", 1)[0] if key.rsplit("_", 1)[-1] in ("contains", "prefix", "suffix") else key
                    parts.append(f"{base}~{rule[key]!r}（实际 {subj.as_dict().get(base,'')[:40]!r}）")
        if not parts:
            parts.append("无条件")
        return f"{source}[{index}]: " + ", ".join(parts)

    # ---------- 判定 ----------

    def decide(self, subj: Subject) -> Decision:
        """四段判定。顺序不能换：deny → approve → escalate → default。"""
        if not self.enabled:
            return Decision("allow", "policy disabled（对照实测，闸门未生效）",
                            rule_source="disabled", matched=subj.as_dict())

        # 1. deny 最先。一条 allow 不许盖掉 deny。
        hit = self._first_hit(self.auto_deny, subj, "auto_deny")
        if hit:
            rule, idx = hit
            return Decision("deny", self._describe(rule, subj, "auto_deny", idx),
                            rule_index=idx, rule_source="auto_deny", matched=subj.as_dict())

        # 2. approve
        hit = self._first_hit(self.auto_approve, subj, "auto_approve")
        if hit:
            rule, idx = hit
            return Decision("allow", self._describe(rule, subj, "auto_approve", idx),
                            rule_index=idx, rule_source="auto_approve", matched=subj.as_dict())

        # 3. escalate：交给人。前端 P9 卡片接这个。
        hit = self._first_hit(self.escalate, subj, "escalate")
        if hit:
            rule, idx = hit
            return Decision("escalate", self._describe(rule, subj, "escalate", idx),
                            rule_index=idx, rule_source="escalate", matched=subj.as_dict())

        # 4. 兜底。没有规则命中时按 default_action，而不是"放行"。
        return Decision(self.default_action,
                        f"没有规则命中，按 default_action={self.default_action}",
                        rule_source="default_action", matched=subj.as_dict())

    def record(self, decision: Decision) -> None:
        self.hits[decision.kind] = self.hits.get(decision.kind, 0) + 1

    # ---------- 可观测 ----------

    def effective_config(self) -> dict:
        """当前生效的规则表。GET /api/policy 给前端/自检用。"""
        return {
            "enabled": self.enabled,
            "default_action": self.default_action,
            "auto_deny": self.auto_deny,
            "auto_approve": self.auto_approve,
            "escalate": self.escalate,
            "warnings": self.warnings,
            "hits": dict(self.hits),
            "rule_count": len(self.auto_deny) + len(self.auto_approve) + len(self.escalate),
        }

    def summary(self) -> str:
        """一行摘要，启动时打日志用。"""
        state = "启用" if self.enabled else "**已禁用（不过闸）**"
        return (f"[policy] {state} deny={len(self.auto_deny)} "
                f"approve={len(self.auto_approve)} escalate={len(self.escalate)} "
                f"default={self.default_action}")


def build_config_for_agent(agent_id: str, rules: Iterable[dict]) -> list[dict]:
    """按 agent 过滤规则（可选能力，供 room 覆盖单个 agent 的策略）。"""
    return [r for r in rules if not r.get("agent") or r.get("agent") == agent_id]
