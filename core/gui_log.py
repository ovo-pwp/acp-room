"""日志：Windows 下编码很坑，但坑的方向和直觉相反。

原来这里做的是「先 encode 成 stderr 的编码再 decode」——本意是防止
GBK 控制台炸。可一旦 stderr 被**重定向到文件**（我们的启动方式），
Python 报的 `sys.stderr.encoding` 是 cp936，于是中文全被替换成 `?`：

    [10:01:00] [pi] ????????stopReason=end_turn????????? empty

消息本身被毁掉了，比不打印还糟——"保护"变成了"损坏"。

正确做法是**不改动消息**，只保证写出去不抛：重配 stderr 为 utf-8，
再兜一层 errors="replace"，让编码错误只影响写不出来的那几个字节。

    reconfigure(encoding="utf-8")   # 消息保持原样
    except UnicodeEncodeError:      # 实在不行也不炸
        ...
"""

from __future__ import annotations

import sys
import time


def _prepare() -> None:
    """把 stderr 切到 utf-8，且永不抛异常。

    只在支持的流上调用（TextIOWrapper 有 reconfigure，StringIO 之类没有）。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


_prepare()


def log(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    try:
        print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        # 兜底：真写不出去时降级，但不再让"保护"毁掉消息本体。
        try:
            enc = sys.stderr.encoding or "utf-8"
            print(f"[{stamp}] {msg.encode(enc, 'replace').decode(enc, 'replace')}",
                  file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
