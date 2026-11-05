"""core/policy.py 的单测 —— 策略闸门。

关键约束（每条都有对应用例）：
  1. 顺序 deny → approve → escalate → default，不能换
  2. fail-closed：没规则命中就按 default_action，默认是 deny
  3. 坏规则丢弃并告警，**不**"匹配一切"
  4. 拒绝必须带人话理由（rule 字段）
"""

import pytest

from core.policy import Decision, Policy, Subject


# 每个用例都用 from_rules：**不读 config.json**。
# 不能用 Policy(config=...)：那会把磁盘上的规则叠加进来，改一次线上配置就跑挂一片测试。
def mk(**kw):
    base = {"auto_deny": [], "auto_approve": [], "escalate": [], "default_action": "deny"}
    base.update(kw)
    return Policy.from_rules(base)


# ---------------------------------------------------------------- 四段顺序

def test_deny最先_一条allow盖不掉deny():
    p = mk(auto_deny=[{"path_prefix": "C:/Windows"}],
           auto_approve=[{"tool": "execute"}])
    d = p.decide(Subject(tool="execute", path="C:/Windows/x.exe"))
    assert d.kind == "deny"
    assert d.rule_source == "auto_deny"


def test_approve在escalate之前():
    p = mk(auto_approve=[{"tool": "edit"}], escalate=[{"tool": "edit"}])
    assert p.decide(Subject(tool="edit")).kind == "allow"


def test_escalate在default之前():
    p = mk(escalate=[{"tool": "write"}], default_action="allow")
    assert p.decide(Subject(tool="write")).kind == "escalate"


def test_没有命中走default():
    p = mk(default_action="allow")
    d = p.decide(Subject(tool="whatever"))
    assert d.kind == "allow"
    assert d.rule_source == "default_action"


# ---------------------------------------------------------------- fail-closed

def test_默认配置无条件拒绝():
    """不给任何规则时，必须全拒——这是 fail-closed 的本体。"""
    p = Policy.from_rules({})
    d = p.decide(Subject(tool="read"))
    assert d.kind == "deny"
    assert d.rule_source == "default_action"


def test_坏default_action按deny处理():
    p = mk(default_action="yes")
    assert p.default_action == "deny"
    assert any("default_action" in w for w in p.warnings)


def test_禁用时全放行且说清楚():
    p = Policy.from_rules({"enabled": False})
    d = p.decide(Subject(tool="rm -rf"))
    assert d.kind == "allow"
    assert d.rule_source == "disabled"


# ---------------------------------------------------------------- 匹配能力

@pytest.mark.parametrize("rule,subj,hit", [
    ({"tool": "execute"},        Subject(tool="execute"),                  True),
    ({"tool": "EXECUTE"},        Subject(tool="execute"),                  True),   # 大小写不敏感
    ({"title_contains": "rm"},   Subject(title="please rm the file"),      True),
    ({"title_contains": "RM"},   Subject(title="rm"),                      True),
    ({"title_regex": r"\brm\s+-rf"}, Subject(title="rm  -rf /"),            True),
    ({"path_prefix": "C:/Win"},  Subject(path="C:/Windows/x"),             True),
    ({"path_suffix": ".env"},    Subject(path="a/b/.env"),                 True),
    ({"command_regex": r"\bdel\b"}, Subject(command="del /q x"),           True),
    ({"agent": "codex"},         Subject(agent="codex"),                   True),
    ({},                         Subject(tool="anything"),                 True),   # 无条件
    ({"tool": "read"},           Subject(tool="write"),                    False),
    ({"path_prefix": "D:/"},     Subject(path="C:/x"),                     False),
])
def test_匹配器(rule, subj, hit):
    # 关键：default_action 取 allow。若取 deny，则"没命中"和"命中往严里判"
    # 都会得到 deny，这个用例就永远测不出规则到底命中没有。
    p = mk(auto_deny=[rule], default_action="allow")
    assert (p.decide(subj).rule_source == "auto_deny") is hit


def test_多字段是AND():
    p = mk(auto_deny=[{"tool": "execute", "path_prefix": "C:/Windows"}],
           default_action="allow")
    hit = lambda s: p.decide(s).rule_source == "auto_deny"
    assert hit(Subject(tool="execute", path="C:/Windows/x"))
    assert not hit(Subject(tool="execute", path="D:/x"))
    assert not hit(Subject(tool="read", path="C:/Windows/x"))


# ---------------------------------------------------------------- 坏规则

def test_未知字段的规则被丢弃并告警():
    p = mk(auto_deny=[{"nonsense_field": 1}])
    assert p.warnings
    assert len(p.auto_deny) == 0
    # 丢弃后不匹配任何东西，走 default
    assert p.decide(Subject(tool="x")).rule_source == "default_action"


def test_无效正则被丢弃并告警():
    p = mk(auto_deny=[{"command_regex": "([bad"}])
    assert any("正则无效" in w for w in p.warnings)
    assert p.decide(Subject(tool="x")).rule_source == "default_action"


def test_非数组的规则块被忽略():
    p = mk(auto_deny="not a list")
    assert p.auto_deny == []
    assert any("不是数组" in w for w in p.warnings)


def test_一条坏规则不影响其它规则():
    p = mk(auto_deny=[{"bogus": 1}, {"tool": "execute"}])
    assert len(p.auto_deny) == 1
    assert p.decide(Subject(tool="execute")).kind == "deny"


# ---------------------------------------------------------------- 理由

def test_拒绝带出规则序号和来源():
    p = mk(auto_deny=[{"tool": "a"}, {"tool": "b"}])
    d = p.decide(Subject(tool="b"))
    assert d.kind == "deny"
    assert d.rule_index == 1
    assert d.rule_source == "auto_deny"
    assert "auto_deny[1]" in d.rule


def test_理由里带实际值():
    p = mk(auto_deny=[{"path_prefix": "C:/Windows"}])
    d = p.decide(Subject(path="C:/Windows/System32/a.dll"))
    assert "C:/Windows/System32/a.dll" in d.rule


def test_默认理由说明没有规则命中():
    d = mk().decide(Subject(tool="x"))
    assert "没有规则命中" in d.rule


# ---------------------------------------------------------------- 其它

def test_from_permission解析ACP形状():
    params = {
        "toolCall": {"title": "Write file", "kind": "edit",
                     "rawInput": {"path": "a/b.txt", "command": ["pwsh", "-c", "x"]}},
        "options": [{"optionId": "1", "kind": "allow_once"}],
    }
    s = Subject.from_permission(params, agent="codex", initiator="discussion")
    assert s.tool == "edit"
    assert s.title == "Write file"
    assert s.path == "a/b.txt"
    assert s.command == "pwsh -c x"     # 列表被拼成字符串
    assert s.initiator == "discussion"


def test_from_permission缺字段不炸():
    s = Subject.from_permission({}, agent="pi")
    assert s.agent == "pi"
    assert s.tool == ""
    assert s.initiator == "person"


def test_initiator传进匹配():
    p = mk(auto_deny=[{"initiator": "routine"}], default_action="allow")
    hit = lambda s: p.decide(s).rule_source == "auto_deny"
    assert hit(Subject(tool="x", initiator="routine"))
    assert not hit(Subject(tool="x", initiator="person"))


def test_命中计数():
    p = mk(auto_approve=[{"tool": "read"}])
    for _ in range(3):
        p.record(p.decide(Subject(tool="read")))
    p.record(p.decide(Subject(tool="write")))
    assert p.hits["allow"] == 3
    assert p.hits["deny"] == 1


def test_effective_config可序列化():
    p = mk(auto_deny=[{"tool": "x"}])
    cfg = p.effective_config()
    assert cfg["rule_count"] == 1
    assert cfg["default_action"] == "deny"
    assert isinstance(cfg["warnings"], list)


def test_decision可序列化():
    d = Decision("deny", "理由", rule_index=0, rule_source="auto_deny")
    assert d.as_dict()["kind"] == "deny"
    assert d.allowed is False
