"""Default parse(name, bytes) -> (message blocks, summary); no UI or session state."""

from __future__ import annotations

import base64
import json
from typing import Any

from kaipi.i18n import tr

MAX_TEXT = 128 * 1024
MAX_FILE = 5 * 1024 * 1024
TAG = "<kaipi:attachment "


def parse(name: str, raw: bytes) -> tuple[list[dict[str, Any]], str]:
    # All decoder dependencies stay behind this callable and load only on attachment use.
    if raw.startswith(b"%PDF-"):
        if len(raw) > MAX_FILE:
            raise ValueError(tr("PDF 超过模型可发送的 5 MiB 上限"))
        return [
            {
                "type": "document",
                "title": name,
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(raw).decode(),
                },
            }
        ], "PDF · 原始文档"
    from . import images

    decoded = images.images(raw)
    title = json.dumps(name, ensure_ascii=False)
    if decoded is not None:
        blocks, summary = decoded
        if "→" in summary:
            blocks.append(
                {"type": "text", "text": f"{TAG}name={title}>{summary}</kaipi:attachment>"}
            )
        return blocks, summary
    from kaipi.providers import redact

    from .documents import text as extract

    text = extract(name, raw)
    if "\0" in text:
        raise ValueError(tr("无法解析此二进制附件：{name}", name=name))
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ValueError(tr("文本附件过大（最多 128 KiB）：{name}", name=name))
    return [
        {"type": "text", "text": f"{TAG}name={title}>\n{redact(text)}\n</kaipi:attachment>"}
    ], "文本／文档 · 已提取内容"
