"""日志编码：别再让"保护"毁掉消息。

原始 bug 的形状是——stderr 重定向到文件时 `sys.stderr.encoding` 是 cp936，
旧实现把消息 encode/decode 一轮，中文全变 `?`：

    [10:01:00] [pi] ????????stopReason=end_turn????????? empty

日志是排查的唯一线索，被毁掉比没有更糟：它看起来像正常输出。
"""

from __future__ import annotations

import io
import sys

from core.gui_log import log


def test_中文不被替换成问号(capsys):
    log("[pi] 空正文（stopReason=end_turn），记为 empty")
    err = capsys.readouterr().err
    assert "空正文" in err
    assert "记为 empty" in err
    assert "?" not in err


def test_日志带时间戳前缀(capsys):
    log("hello")
    err = capsys.readouterr().err
    assert "hello" in err
    # [HH:MM:SS] 前缀
    assert err.lstrip().startswith("[") and "]" in err


def test_特殊字符无损(capsys):
    """箭头、全角括号、emoji、书名号——发言里全都会出现。"""
    msg = "第 4/8 步 → pi（addressed：上一句在跟 pi 说话）✅「引用」"
    log(msg)
    assert msg in capsys.readouterr().err


def test_写不出去也不抛异常(monkeypatch):
    """日志永远不该成为故障源。"""

    class Boom(io.StringIO):
        def write(self, *_a, **_k):
            raise OSError("磁盘满了")

    monkeypatch.setattr(sys, "stderr", Boom())
    log("随便什么")  # 不抛就算过


def test_编码失败时降级而不是崩溃(monkeypatch):
    class Strict(io.StringIO):
        encoding = "ascii"

        def write(self, s):
            s.encode("ascii")  # 中文会抛
            return super().write(s)

    monkeypatch.setattr(sys, "stderr", Strict())
    log("中文内容")  # 不抛就算过
