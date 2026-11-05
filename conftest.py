"""pytest 公共夹具。

conftest 放在 acp-room/ 根，让 `python -m pytest` 在任何目录下都能从项目根 import core.*。
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class FakeTurn:
    """最小 Turn 替身。

    刻意**不** import room.Turn：core 的单测不该因为 room.py 改了个字段就挂掉，
    那样测的就不是 core 了。这正是本套测试要证明的事——core 能脱离宿主跑。
    """

    seq: int
    speaker: str
    name: str
    text: str
    status: str = "ok"
    tokens: int = 0        # ACP 报的是**会话累计值**，不是单轮值
    attachments: list = field(default_factory=list)


@pytest.fixture
def turn_factory():
    def make(seq, speaker, text, name=None, **kw):
        return FakeTurn(seq=seq, speaker=speaker, name=name or speaker.title(), text=text, **kw)
    return make
