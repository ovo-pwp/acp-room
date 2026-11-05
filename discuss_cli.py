"""讨论引擎的命令行试跑入口。

    python -m core.discussion --method standard "问题"
    python discuss_cli.py --method oxford --trace "远程办公好不好"

## 为什么从 core/discussion.py 搬出来

这段代码 `import room`，而 `core/` 有一条硬约束：**不许依赖宿主**
（见 `tests/test_architecture.py`）。原来它藏在 `core/discussion.py` 的 `_main()` 里，
结果整个文件都被判为"依赖宿主"，于是**整个讨论引擎都无法单测**。

搬到这里之后：`core/discussion.py` 不再 import room，收敛判据
（`core/convergence.py`）可以脱离宿主任意单测。

CLI 属于宿主层——它本来就要构造 Room、连 4 个 ACP 进程，放在根目录才对。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.discussion import Budget, Discussion  # noqa: E402
from core.gui_log import log  # noqa: E402
import room as R  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    r = R.Room(ROOT)
    await r.connect_all()
    await r.say_human(args.topic)

    budget = Budget(max_rounds=args.rounds)
    if args.threshold is not None:
        budget.no_progress_threshold = args.threshold
    if args.min_marker_rounds is not None:
        budget.min_rounds_before_marker = args.min_marker_rounds

    disc = Discussion(r, args.method, budget)
    verdict = await disc.run(args.topic, args.speakers or r.order)
    print(f"\nSTOP: {verdict.kind} — {verdict.reason}")
    print(f"tokens={disc.tokens_used}")
    if args.trace:
        print("TRACE " + json.dumps(disc.trace, ensure_ascii=False))
    await r.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑一场讨论并打印停因与 token 消耗")
    ap.add_argument("topic")
    ap.add_argument("--method", default="standard",
                    choices=["standard", "advocate", "oxford"])
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--speakers", nargs="*")
    ap.add_argument("--trace", action="store_true",
                    help="结束时打印每轮交锋的相似度轨迹 JSON（标定 no_progress 用）")
    ap.add_argument("--threshold", type=float, default=None,
                    help="覆盖 no_progress 重合度阈值（默认取 Budget.no_progress_threshold）")
    ap.add_argument("--min-marker-rounds", type=int, default=None,
                    help="覆盖标记生效前最少交锋轮数（调大可观察完整五阶段流程）")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log("被用户中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
