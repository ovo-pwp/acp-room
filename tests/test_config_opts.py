"""模型切换走哪个 ACP 方法，以及 model_of 的取材。

ACP 有两个模型口：新适配器给 `configOptions`（走 `session/set_config_option`），
老适配器只给 `models.availableModels`（只有 `session/set_model`，不收 configId）。
选错口的症状很难查：请求发出去、适配器回 method not found，界面上只是"切不动"。

所以 normalize 给每组配置打上 via 标记，set_option 按标记分路。这里锁住这两条路。
"""

import asyncio
import json

import pytest

from core.config_opts import ConfigStore, normalize


class FakeCfg:
    def __init__(self, aid):
        self.id = aid


class FakeAgent:
    """只记录发了什么、按 ACP 的样式回。"""

    def __init__(self, aid="pi", sid="S-1", reachable_value="gpt-x"):
        self.cfg = FakeCfg(aid)
        self.session_id = sid
        self.calls: list[tuple[str, dict]] = []
        self.reachable_value = reachable_value

    async def request(self, method, params, **kw):
        self.calls.append((method, params))
        if method == "session/set_config_option":
            return {"configOptions": [{
                "id": params["configId"],
                "category": "model",
                "currentValue": self.reachable_value,
                "options": [{"value": self.reachable_value}],
            }]}
        return {}

    @property
    def methods(self):
        return [m for m, _ in self.calls]


NEW_STYLE = {
    "configOptions": [{
        "id": "model", "name": "Model", "category": "model",
        "currentValue": "glm-5", "options": [{"value": "glm-5"}, {"value": "gpt-x"}],
    }],
}

LEGACY_STYLE = {
    "models": {
        "currentModelId": "old-a",
        "availableModels": [{"modelId": "old-a", "name": "Old A"},
                            {"modelId": "old-b", "name": "Old B"}],
    }
}


def test_新旧两种来源都归一但via标记不同():
    a = normalize(NEW_STYLE)
    b = normalize(LEGACY_STYLE)
    assert a[0]["via"] == "config"
    assert b[0]["via"] == "legacy"
    assert b[0]["currentValue"] == "old-a"
    assert [o["value"] for o in b[0]["options"]] == ["old-a", "old-b"]


def test_configOptions里有model时老字段不抢占():
    """适配器两个都给（pi / codex 实测就是这样）。必须认 configOptions——
    它才是当前会话真正能切的那份，老字段只是历史遗留。"""
    both = {"configOptions": NEW_STYLE["configOptions"], **LEGACY_STYLE}
    out = normalize(both)
    models = [o for o in out if o["category"] == "model"]
    assert len(models) == 1
    assert models[0]["via"] == "config"


def test_老适配器走session_set_model且绝不发config_option(tmp_path):
    store = ConfigStore(tmp_path)
    agent = FakeAgent("pi")
    store.for_agent("pi").update(LEGACY_STYLE)

    entry = asyncio.run(store.set_option(agent, "model", "old-b"))

    assert agent.methods == ["session/set_model"]
    assert agent.calls[0][1] == {"sessionId": "S-1", "modelId": "old-b"}
    assert entry["verified"] is True
    assert entry["currentValue"] == "old-b"
    # 切成功要落盘，否则重启又回到默认模型
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {
        "pi": {"model": "old-b"}}


def test_新适配器切成功时verified为真并落盘(tmp_path):
    store = ConfigStore(tmp_path)
    agent = FakeAgent("omp", reachable_value="gpt-x")
    store.for_agent("omp").update(NEW_STYLE)

    entry = asyncio.run(store.set_option(agent, "model", "gpt-x"))

    assert agent.methods == ["session/set_config_option"]
    assert "session/set_model" not in agent.methods
    assert entry["verified"] is True


def test_适配器回包不认这个值时verified必须为假(tmp_path):
    """verified 只认回包里的 currentValue——它才是"切没切"的唯一证据。

    实测过反过来的一面：网关上根本没有那个模型，适配器照样回成功。
    """
    store = ConfigStore(tmp_path)
    agent = FakeAgent("omp", reachable_value="glm-5")   # 适配器仍停在原模型
    store.for_agent("omp").update(NEW_STYLE)

    entry = asyncio.run(store.set_option(agent, "model", "gpt-x"))

    assert entry["verified"] is False
    assert entry["currentValue"] == "glm-5"
    # 没切成不该写预置，否则下次启动又去试同一个不可能的值
    assert not (tmp_path / "state.json").exists()


def test_拒绝不在列表里的模型(tmp_path):
    store = ConfigStore(tmp_path)
    agent = FakeAgent("pi")
    store.for_agent("pi").update(NEW_STYLE)
    with pytest.raises(ValueError, match="不接受"):
        asyncio.run(store.set_option(agent, "model", "nope"))
    assert agent.methods == []


def test_model_of读当前值且绝不为查询创建条目(tmp_path):
    """人类发言也走 model_of（room._commit 不区分说话人）。
    若它顺手建了条目，"human" 会混进 snapshot()/brief()，界面上多出一个假 agent。"""
    store = ConfigStore(tmp_path)
    store.for_agent("pi").update(NEW_STYLE)

    assert store.model_of("pi") == "glm-5"
    assert store.model_of("human") == ""
    assert list(store.by_agent) == ["pi"]


def test_brief里的model和model_of同源(tmp_path):
    """侧栏每轮广播读的是 brief()，发言戳读的是 model_of()。
    两个来源不一致的话，界面上「当前模型」和「这句用的模型」会打架。"""
    store = ConfigStore(tmp_path)
    store.for_agent("pi").update(NEW_STYLE)
    assert store.brief()["pi"]["model"] == store.model_of("pi")
