"""core/attachments.py 的单测 —— 图片落盘与 ACP 内容块。

用 tmp_path 作为根，不碰真实的 data/main/attachments。
重点覆盖三类安全边界：
  1. 只收白名单格式，且按魔数判断（不信前端声明的 mime）
  2. 路径穿越打不穿（id 只认 16 位十六进制）
  3. 大小上限
"""

import base64
import struct
import zlib

import pytest

from core.attachments import MAX_BYTES, Attachments, AttachmentError, sniff_mime


def make_png(w=4, h=4, rgb=(200, 40, 40)):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


PNG = make_png()
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.fixture
def att(tmp_path):
    return Attachments(tmp_path, "testroom")


# ---------------------------------------------------------------- sniff_mime

@pytest.mark.parametrize("data,expect", [
    (b"\x89PNG\r\n\x1a\nxxxx", "image/png"),
    (b"\xff\xd8\xff\xe0xxxx", "image/jpeg"),
    (b"GIF89a....", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
])
def test_按魔数识别(data, expect):
    assert sniff_mime(data) == expect


def test_认不出但有白名单声明就用声明():
    assert sniff_mime(b"garbage", "image/png") == "image/png"


def test_认不出且声明不在白名单就拒绝():
    with pytest.raises(AttachmentError):
        sniff_mime(b"garbage", "image/bmp")


def test_认不出且没声明就拒绝():
    with pytest.raises(AttachmentError):
        sniff_mime(b"garbage")


def test_声明撒谎以魔数为准():
    """前端说 png，实际是 gif —— 必须按 gif 处理，不能信声明。"""
    assert sniff_mime(b"GIF89a....", "image/png") == "image/gif"


# ---------------------------------------------------------------- save

def test_保存data_url(att):
    info = att.save(PNG_URL, "red.png")
    assert info["mime"] == "image/png"
    assert info["bytes"] == len(PNG)
    assert info["name"] == "red.png"
    assert info["url"] == f"/api/attachment/{info['id']}"


def test_保存裸base64(att):
    info = att.save(base64.b64encode(PNG).decode())
    assert info["mime"] == "image/png"


def test_id是内容哈希_重复保存去重(att):
    a = att.save(PNG_URL)
    b = att.save(PNG_URL, "另一个名字.png")
    assert a["id"] == b["id"]
    # 磁盘上只应该有一个文件
    assert len(list(att.dir.glob(f"{a['id']}.*"))) == 1


def test_内容不同id不同(att):
    a = att.save(PNG_URL)
    other = make_png(rgb=(10, 200, 10))
    b = att.save("data:image/png;base64," + base64.b64encode(other).decode())
    assert a["id"] != b["id"]


def test_空内容被拒(att):
    with pytest.raises(AttachmentError, match="为空"):
        att.save("")


def test_非法base64被拒(att):
    with pytest.raises(AttachmentError, match="base64"):
        att.save("data:image/png;base64,!!!not base64!!!")


def test_非图片被拒(att):
    with pytest.raises(AttachmentError, match="格式"):
        att.save(base64.b64encode(b"I am not an image").decode())


def test_超限被拒(att, monkeypatch):
    # 造一个"看起来"超限的字节串：魔数对，长度超 MAX_BYTES
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_BYTES + 10)
    with pytest.raises(AttachmentError, match="太大"):
        att.save(base64.b64encode(big).decode())


def test_文件名被截断(att):
    info = att.save(PNG_URL, "x" * 200)
    assert len(info["name"]) == 80


# ---------------------------------------------------------------- read / path_of

def test_读回来字节一致(att):
    info = att.save(PNG_URL)
    got = att.read(info["id"])
    assert got is not None
    raw, mime = got
    assert raw == PNG
    assert mime == "image/png"


def test_不存在的id返回None(att):
    assert att.read("0" * 16) is None


@pytest.mark.parametrize("bad", [
    "../../config",              # 穿越
    "..",                        # 相对
    "C:/Windows/system32",       # 绝对
    "abc",                       # 太短
    "ZZZZZZZZZZZZZZZZ",          # 非十六进制
    "",                          # 空
    "0123456789abcdef0",         # 太长
])
def test_路径穿越与非法id打不穿(att, bad):
    assert att.path_of(bad) is None
    assert att.read(bad) is None


# ---------------------------------------------------------------- to_acp_blocks

def test_转成ACP图片块(att):
    info = att.save(PNG_URL)
    blocks = att.to_acp_blocks([info])
    assert len(blocks) == 1
    b = blocks[0]
    # 形状对得上 acpx/src/prompt-content.ts 的校验要求
    assert b["type"] == "image"
    assert b["mimeType"] == "image/png"
    assert base64.b64decode(b["data"]) == PNG


def test_不存在的附件被跳过(att):
    assert att.to_acp_blocks([{"id": "0" * 16}]) == []


def test_空列表返回空(att):
    assert att.to_acp_blocks([]) == []


def test_多张图保持顺序(att):
    a = att.save(PNG_URL)
    b = att.save("data:image/png;base64," + base64.b64encode(make_png(rgb=(1, 2, 3))).decode())
    blocks = att.to_acp_blocks([a, b])
    assert [base64.b64decode(x["data"]) for x in blocks] == [PNG, make_png(rgb=(1, 2, 3))]
