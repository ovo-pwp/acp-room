"""架构守卫 —— 保证 core/ 可脱离宿主单测。

这是抄 `dsh-roundtable` 的那条硬约束：

    src/core 不 import 任何 @deepseek-ai/*，可脱离宿主单测

对应到本项目：**`core/` 里的模块不许 import `room`、不许 import 任何 ACP 传输实现**。

为什么值得单独一个测试文件：这条约束一旦破掉，不会有人立刻发现——
代码照跑，只是「想验证上下文组装就得起 4 个 ACP 进程」这件事悄悄回来了。
守卫测试把它变成一条会在 CI 里响的红灯。

注意：`core/agent.py` 是例外——它**是** ACP 传输层，本来就要起进程。
所以白名单里显式列出它，而不是放宽整条规则。
"""

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent / "core"

# 允许 import 宿主模块的文件。
# 只有真的必须才行——每加一个，就少一块能被单测保护的区域。
ALLOWED = {
    "agent_admin.py",    # 探活要复用 ACP 客户端（from core.agent import ...）
    "config_opts.py",    # 模型切换要起 ACP 进程（from core.agent import ...）
}

# 纯函数模块：必须能完全脱离宿主。
# 这些是「想验证就必须起 4 个 ACP 进程」被消除掉的那部分。
PURE = [
    "arbitration.py",
    "attachments.py",
    "context.py",
    "policy.py",
    "store.py",
    "audit.py",
    "gui_log.py",
    "agent.py",          # ACP 客户端本体，它是最底层，不依赖 room/server
    "skills.py",         # 技能扫描/注入，只有文件 IO
    "convergence.py",    # 收敛判据状态机（从 discussion.py 抽出来的）
    "discussion.py",     # 编排：只调 room 的公开接口，CLI 入口已搬到根目录
    "bench.py",          # 算力基准：只依赖 stdlib + core.gui_log
    "persona.py",        # 身份与人格：纯字符串拼接，零依赖
    "model_probe.py",    # 模型探活：只依赖 httpx + core.gui_log（不发 ACP 请求）
    "turntaking.py",     # 发言顺序：纯函数，零依赖
    "memory.py",         # 房间记忆：只有文件 IO，不碰 room / ACP
    "agent_memory.py",   # 推进 agent 自己的记忆文件：只有 stdlib 文件 IO，落点各家不同
    "reply_guard.py",    # 回复守卫：判一条输出是不是"真的在说话"，纯正则 + json
]

HOST_MODULES = {"room", "server"}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
            # from . import x  / from ..x import y
            if node.level:
                out.add("__relative__")
    return out


def imports_full(path: Path) -> set[str]:
    """完整的模块名（含点），用于检查 `from core.agent import ...` 这种。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


@pytest.mark.parametrize("name", PURE)
def test_纯函数模块不依赖宿主(name):
    path = CORE / name
    assert path.exists(), f"{name} 不存在"
    mods = imports_of(path)
    assert not (mods & HOST_MODULES), (
        f"core/{name} 依赖了宿主模块 {mods & HOST_MODULES}。"
        f"core 必须能脱离 Room 单测——请把这段逻辑抽成纯函数。"
    )


@pytest.mark.parametrize("name", PURE)
def test_纯函数模块不引用ACP传输(name):
    """`from core.agent import ...` 会在 import 时把 ACP 客户端拉进来。"""
    full = imports_full(CORE / name)
    assert "core.agent" not in full, (
        f"core/{name} import 了 core.agent（ACP 传输）。"
        f"那会让单测在 import 阶段就依赖宿主能力。"
    )


def test_白名单里的文件确实存在():
    for name in ALLOWED:
        assert (CORE / name).exists(), f"白名单里的 core/{name} 不存在（白名单过期了）"


def test_白名单没有多余项():
    """白名单只应包含确实需要宿主的文件。多出来的项等于给自己开了后门。"""
    for name in ALLOWED:
        full = imports_full(CORE / name)
        uses_host = bool(imports_of(CORE / name) & HOST_MODULES) or "core.agent" in full
        assert uses_host, (
            f"core/{name} 在白名单里，但实际没有依赖宿主——应把它移出白名单，"
            f"让它也受守卫保护。"
        )


def test_core目录下的每个文件都在名单里():
    """新加的 core 模块必须明确归入 PURE 或 ALLOWED，不能默认放行。"""
    known = set(PURE) | ALLOWED | {"__init__.py"}
    actual = {p.name for p in CORE.glob("*.py")}
    unknown = actual - known
    assert not unknown, (
        f"core/ 下有未分类的模块 {sorted(unknown)}。"
        f"请在 tests/test_architecture.py 里把它们归入 PURE 或 ALLOWED。"
    )


def test_room不import测试用假对象():
    """room.py 不许为了"方便测试"去 import tests 里的东西。"""
    room = Path(__file__).resolve().parent.parent / "room.py"
    mods = imports_of(room)
    assert "tests" not in mods
