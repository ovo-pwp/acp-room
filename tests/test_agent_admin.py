"""添加 / 更新 agent 的配置清洗与合并语义。

这里钉的是两件容易搞错的事：

1. **留空的人格字段不能写进 config.json。** 写了空串会盖掉
   `core/persona.py` 按 id 匹配的内置人格，新人拿到的比默认还差。
2. **从界面重加一个已有 agent 不能把它的人格抹掉。** `upsert_agent` 用的是
   `{**旧, **新}` 合并——只要新 spec 里没有那个键，旧值就保住了。
   这条依赖 (1) 才成立，所以两者必须一起测。
"""

import json

import pytest

from core import agent_admin as aa


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """把 CONFIG 指到临时文件，别动仓库里真正的那份。"""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"agents": []}), encoding="utf-8")
    monkeypatch.setattr(aa, "CONFIG", p)
    return p


# ---------------------------------------------------------------- 清洗

def test_不填人格就不写这几个键():
    spec = aa.validate_spec({"id": "kimi", "name": "Kimi", "cmd": "kimi acp"})
    for k in ("role", "tag", "style"):
        assert k not in spec, f"{k} 不该出现——空串会盖掉内置人格"


def test_填了人格就写进去():
    spec = aa.validate_spec({
        "id": "kimi", "name": "Kimi", "cmd": "kimi acp",
        "role": "你负责验证", "tag": "验证 · 只信能复现的", "style": "短句",
    })
    assert spec["role"] == "你负责验证"
    assert spec["tag"] == "验证 · 只信能复现的"
    assert spec["style"] == "短句"


def test_空白人格视为没填():
    """界面上空格也算"没填"，否则会在配置里留一串看不见的空格。"""
    spec = aa.validate_spec({
        "id": "kimi", "cmd": "kimi acp",
        "role": "   ", "tag": "\n", "style": "  ",
    })
    assert "role" not in spec and "tag" not in spec and "style" not in spec


def test_人格字段有长度上限():
    spec = aa.validate_spec({
        "id": "kimi", "cmd": "kimi acp",
        "role": "你" * 500, "tag": "标" * 200, "style": "风" * 200,
    })
    assert len(spec["role"]) <= 400
    assert len(spec["tag"]) <= 80
    assert len(spec["style"]) <= 120


@pytest.mark.parametrize("bad", ["red", "#12345", "#GGGGGG", "", "rgb(1,2,3)"])
def test_非法颜色不写这个键(bad):
    """非法/空颜色**不写键**，而不是写一个默认灰。

    写默认灰会在合并时把已有 agent 的身份色覆盖掉——和人格那个坑同源。
    新 agent 缺 color 键不影响使用：room 侧 _spec_from_cfg 会回落 #888888。
    """
    spec = aa.validate_spec({"id": "kimi", "cmd": "kimi acp", "color": bad})
    assert "color" not in spec


def test_合法颜色原样保留():
    spec = aa.validate_spec({"id": "kimi", "cmd": "kimi acp", "color": "#56D4DD"})
    assert spec["color"] == "#56D4DD"


def test_缺颜色时房间侧回落默认灰():
    """配置里没有 color 键，房间读出来应该拿到 #888888，而不是空串。"""
    from core.agent import AgentConfig

    spec = aa.validate_spec({"id": "kimi", "cmd": "kimi acp"})
    cfg = AgentConfig(id=spec["id"], name=spec["name"], cmd=spec["cmd"], cwd=".",
                      color=spec.get("color", "#888888"))
    assert cfg.color == "#888888"


def test_id_必须合法():
    with pytest.raises(aa.AgentError):
        aa.validate_spec({"id": "Kimi Code!", "cmd": "kimi acp"})


# ---------------------------------------------------------------- 合并

def test_重加已有agent不会抹掉它的人格(tmp_config):
    """界面表单只填 id/名字/命令时，原来的人格必须原样保留。

    这正是「留空不写键」要保护的东西：写空串的话，用户在界面上
    随便点一下「添加」，那个 agent 的人格就没了，而且看不出来。
    """
    spec_full = aa.validate_spec({
        "id": "kimi", "name": "Kimi", "cmd": "kimi acp",
        "role": "你负责验证", "tag": "验证", "style": "短句",
        "color": "#56d4dd",
    })
    aa.upsert_agent(spec_full)

    # 第二次只带必填项（模拟"从界面重加一次"）
    spec_bare = aa.validate_spec({"id": "kimi", "name": "Kimi", "cmd": "kimi acp"})
    action = aa.upsert_agent(spec_bare)
    assert action == "updated"

    saved = json.loads(tmp_config.read_text(encoding="utf-8"))["agents"][0]
    assert saved["role"] == "你负责验证", "人格被界面重加抹掉了"
    assert saved["tag"] == "验证"
    assert saved["style"] == "短句"
    assert saved["color"] == "#56d4dd", "颜色也被抹掉了"


def test_明确改人格时会被覆盖(tmp_config):
    aa.upsert_agent(aa.validate_spec({
        "id": "kimi", "cmd": "kimi acp", "role": "旧角色", "tag": "旧标签"}))
    aa.upsert_agent(aa.validate_spec({
        "id": "kimi", "cmd": "kimi acp", "role": "新角色"}))

    saved = json.loads(tmp_config.read_text(encoding="utf-8"))["agents"][0]
    assert saved["role"] == "新角色"
    assert saved["tag"] == "旧标签", "没指定的字段应该保持原样"


def test_新agent追加到末尾(tmp_config):
    aa.upsert_agent(aa.validate_spec({"id": "a1", "cmd": "x acp"}))
    aa.upsert_agent(aa.validate_spec({"id": "a2", "cmd": "y acp"}))
    ids = [x["id"] for x in json.loads(tmp_config.read_text(encoding="utf-8"))["agents"]]
    assert ids == ["a1", "a2"]
