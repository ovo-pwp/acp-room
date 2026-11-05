"""core/arbitration.py 的单测 —— 发言权仲裁。

全部纯函数，不启动进程、不读配置。
"""

import pytest

from core.arbitration import arbitrate, resolve_at

ROSTER = [("pi", "Pi"), ("omp", "OMP"), ("claude", "Claude Code"), ("codex", "Codex")]


# ---------------------------------------------------------------- resolve_at

@pytest.mark.parametrize("text,expect", [
    ("@codex 你看", ["codex"]),
    ("@claude 和 @omp 对比", ["claude", "omp"]),
    ("@cl 单独", ["claude"]),                 # 唯一前缀
    ("@PI 大写", ["pi"]),                     # 大小写不敏感
    ("没有 at", []),
    ("@all 大家", []),                        # @all 被忽略，走后续规则
    ("@nobody 你", []),                       # 不存在的人
])
def test_resolve_at(text, expect):
    assert resolve_at(text, ROSTER) == expect


def test_歧义前缀不猜():
    """@c 能匹配 claude 和 codex。歧义时返回空，让人写清楚，而不是随机挑一个。"""
    assert resolve_at("@c 你说", ROSTER) == []


def test_重复点名去重():
    assert resolve_at("@pi @pi @pi", ROSTER) == ["pi"]


def test_邮箱不被当成点名():
    # @ 后跟了合法字符会被匹配；这条记录的是当前行为，
    # 不是理想行为。真要修，得先确认没有 agent id 会撞邮箱域名。
    assert resolve_at("mail me at a@pi.com", ROSTER) == ["pi"]


# ---------------------------------------------------------------- 四段优先级

def test_第一段_点名独占():
    g = arbitrate(text="@omp 你来", explicit=None, roster=ROSTER)
    assert g.speakers == ["omp"]
    assert g.reason == "named"


def test_点名优先于回复对象():
    g = arbitrate(text="@omp 你来", explicit=None, roster=ROSTER, reply_to="pi")
    assert g.speakers == ["omp"]
    assert g.reason == "named"


def test_第二段_回复对象():
    g = arbitrate(text="继续", explicit=None, roster=ROSTER, reply_to="claude")
    assert g.speakers == ["claude"]
    assert g.reason == "reply_to"


def test_回复对象不在房间时跳过():
    g = arbitrate(text="继续", explicit=None, roster=ROSTER, reply_to="ghost")
    assert g.reason != "reply_to"


def test_第三段_职责命中_提到id():
    g = arbitrate(text="codex 刚才说的有问题", explicit=None, roster=ROSTER)
    assert g.speakers == ["codex"]
    assert g.reason == "role"


def test_第三段_职责命中_提到名字():
    g = arbitrate(text="Claude Code 你怎么看", explicit=None, roster=ROSTER)
    assert g.speakers == ["claude"]
    assert g.reason == "role"


def test_第三段_多个命中都算():
    g = arbitrate(text="pi 和 codex 谁对", explicit=None, roster=ROSTER)
    assert set(g.speakers) == {"pi", "codex"}
    assert g.reason == "role"


def test_第四段_活跃度补位最少的先来():
    g = arbitrate(text="随便聊聊", explicit=None, roster=ROSTER,
                  activity={"omp": 9, "pi": 5, "claude": 2, "codex": 0})
    assert g.reason == "activity"
    assert g.speakers[0] == "codex"


def test_活跃度缺省数据时按房间顺序():
    g = arbitrate(text="随便聊聊", explicit=None, roster=ROSTER)
    assert g.reason == "all"
    assert g.speakers == ["pi", "omp", "claude"]


# ---------------------------------------------------------------- explicit 最高优先

def test_显式指定覆盖一切():
    g = arbitrate(text="@codex 你看", explicit=["pi"], roster=ROSTER)
    assert g.speakers == ["pi"]
    assert g.reason == "explicit"


def test_显式空列表表示这轮谁都别说():
    g = arbitrate(text="随便", explicit=[], roster=ROSTER)
    assert g.speakers == []
    assert g.reason == "explicit"


def test_显式指定里有不存在的id会被剔除():
    g = arbitrate(text="x", explicit=["pi", "ghost"], roster=ROSTER)
    assert g.speakers == ["pi"]
    assert "ghost" in g.detail


# ---------------------------------------------------------------- 上限

def test_数量上限截断():
    g = arbitrate(text="@pi @omp @claude @codex 都来说", explicit=None,
                  roster=ROSTER, max_speakers=2)
    assert len(g.speakers) == 2


def test_上限对显式指定不截断():
    """人明确点了四个，就该叫四个。上限只约束"替你决定"的那几步。"""
    g = arbitrate(text="x", explicit=["pi", "omp", "claude", "codex"], roster=ROSTER, max_speakers=2)
    assert len(g.speakers) == 4


def test_空房间不报错():
    g = arbitrate(text="有人吗", explicit=None, roster=[])
    assert g.speakers == []


def test_mentions参数优先于正文解析():
    g = arbitrate(text="正文里什么都没写", explicit=None, roster=ROSTER, mentions=["omp"])
    assert g.speakers == ["omp"]
    assert g.reason == "named"


def test_grant可序列化进审计():
    g = arbitrate(text="@pi 你", explicit=None, roster=ROSTER)
    d = g.as_dict()
    assert set(d) == {"speakers", "reason", "detail"}
    assert isinstance(d["speakers"], list)
