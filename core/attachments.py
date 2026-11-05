"""附件（图片/文件）落盘与 ACP 内容块构造。

## 为什么单独一个模块

图片要走两条路，两条路的形状不一样：

1. **给 ACP 的**：`{"type": "image", "mimeType": ..., "data": "<base64>"}`
   （`vendor/acpx/src/prompt-content.ts:74-90` 校验的就是这三个字段）
2. **给人和前端的**：一个能直接显示的 URL，`/api/attachment/<id>`

把 base64 塞进 turns.jsonl 是错的——一张 2MB 的截图会让每轮 `snapshot()` 都背着它，
而 `push_state` 一轮要推好几次（`room.snapshot()` 的注释里已经为 opts 踩过这个坑）。
所以**正文只存 id**，字节落在 `data/<room>/attachments/` 下。

## 限制

- 只收常见图片类型：png / jpeg / webp / gif
- 单张上限 8MB（base64 后会膨胀 ~33%，再大 ACP 有的适配器会直接拒）
- 文件名不参与路径（只用 id + 推断出的扩展名），避免 `../` 与非法字符
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from pathlib import Path

# ACP 认的 mime。别扩得太大：不认识的类型 agent 未必处理得比文本更好。
ALLOWED = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_BYTES = 8 * 1024 * 1024

_DATA_URL_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.S | re.I)


class AttachmentError(ValueError):
    """给用户的、能直接显示的失败原因。"""


def sniff_mime(raw: bytes, declared: str | None = None) -> str:
    """按魔数判断类型，不信前端声明的 mime。

    前端说 `image/png` 而实际是别的，是很常见的粘贴来源问题；
    信声明就会把一个 agent 未必认的格式推过去。
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    # 认不出来时退回声明值，但只接受白名单内的
    if declared and declared.lower() in ALLOWED:
        return declared.lower()
    raise AttachmentError("不是支持的图片格式（只收 png / jpeg / webp / gif）")


class Attachments:
    def __init__(self, root: Path, room: str = "main") -> None:
        self.dir = Path(root) / "data" / room / "attachments"

    # ---------- 存 ----------

    def save(self, data_url_or_b64: str, name: str = "") -> dict:
        """存一张图，返回 {id, mime, bytes, url, name}。"""
        m = _DATA_URL_RE.match((data_url_or_b64 or "").strip())
        if m:
            declared, b64 = m.group(1).lower(), m.group(2)
        else:
            declared, b64 = "", data_url_or_b64
        if not b64:
            raise AttachmentError("图片内容为空")

        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise AttachmentError(f"图片不是合法 base64：{exc}") from exc
        if not raw:
            raise AttachmentError("图片内容为空")
        if len(raw) > MAX_BYTES:
            raise AttachmentError(
                f"图片太大（{len(raw)//1024} KB），上限 {MAX_BYTES//1024//1024} MB"
            )

        mime = sniff_mime(raw, declared)
        # 用内容哈希做 id：同一张图重复发不会堆文件，且天然去重。
        aid = hashlib.sha256(raw).hexdigest()[:16]
        ext = ALLOWED[mime]
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{aid}{ext}"
        if not path.exists():
            path.write_bytes(raw)

        return {
            "id": aid, "mime": mime, "bytes": len(raw),
            "url": f"/api/attachment/{aid}", "name": str(name or "")[:80],
        }

    # ---------- 读 ----------

    def path_of(self, aid: str) -> Path | None:
        """按 id 找文件。路径由 id + 白名单扩展名拼出来，不接用户给的路径。"""
        if not re.fullmatch(r"[0-9a-f]{16}", aid or ""):
            return None
        for ext in ALLOWED.values():
            p = self.dir / f"{aid}{ext}"
            if p.is_file():
                return p
        return None

    def read(self, aid: str) -> tuple[bytes, str] | None:
        p = self.path_of(aid)
        if not p:
            return None
        mime = next(m for m, e in ALLOWED.items() if e == p.suffix)
        return p.read_bytes(), mime

    # ---------- 给 ACP ----------

    def to_acp_blocks(self, atts: list[dict]) -> list[dict]:
        """把附件列表转成 ACP prompt 里的 image 内容块。

        顺序保持在正文之后——先给任务，再给图，模型更容易把图当证据而不是当主题。
        """
        blocks: list[dict] = []
        for a in atts or []:
            aid = str(a.get("id") or "")
            got = self.read(aid)
            if not got:
                continue
            raw, mime = got
            blocks.append({
                "type": "image",
                "mimeType": mime,
                "data": base64.b64encode(raw).decode("ascii"),
            })
        return blocks


def new_id() -> str:
    """预留给后续"上传即分配 id"的路径（当前用内容哈希）。"""
    return uuid.uuid4().hex[:16]
