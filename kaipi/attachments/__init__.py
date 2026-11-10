"""Attachment I/O and frozen drafts. Replace parse(name, raw) to swap the decoder."""

from __future__ import annotations

import base64
import json
import shlex
import shutil
import subprocess
import sys
from copy import deepcopy
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict

from kaipi.i18n import attachment_summary, tr

from .parsing import MAX_TEXT as MAX_TEXT
from .parsing import TAG as TAG
from .parsing import parse as parse

MAX_INPUT = 20 * 1024 * 1024
MAX_INPUT_TOTAL = 40 * 1024 * 1024
MAX_TOTAL = 10 * 1024 * 1024
MAX_FILES = 8


class Attachment(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    data: str  # base64; the source file is never re-read on replay

    @classmethod
    def read(cls, path: Path) -> Attachment:
        if not path.is_file():
            raise ValueError(tr("文件不存在或不是普通文件：{name}", name=path.name))
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
        item = cls(name=path.name, data=base64.b64encode(raw).decode())
        item.block()  # validate before adding it to a draft
        return item

    @cached_property
    def _raw(self) -> bytes:
        if len(self.data) > (MAX_INPUT + 2) // 3 * 4:
            raise ValueError(tr("附件过大（原文件单个最多 20 MiB）：{name}", name=self.name))
        try:
            raw = base64.b64decode(self.data, validate=True)
        except ValueError:
            raise ValueError(tr("附件编码无效")) from None
        if len(raw) > MAX_INPUT:
            raise ValueError(tr("附件过大（原文件单个最多 20 MiB）：{name}", name=self.name))
        return raw

    def raw(self) -> bytes:
        return self._raw

    @cached_property
    def _content(self) -> tuple[list[dict[str, Any]], str]:
        return parse(self.name, self.raw())

    @property
    def summary(self) -> str:
        return attachment_summary(self._content[1])

    def blocks(self) -> list[dict[str, Any]]:
        return deepcopy(self._content[0])

    def block(self) -> dict[str, Any]:
        """Primary block for draft labels; transmission must use all blocks()."""
        return self.blocks()[0]

    @property
    def kind(self) -> str:
        return "image" if self._content[0][0]["type"] == "image" else "file"

    def describe(self) -> dict[str, str]:
        return {"name": self.name, "summary": self.summary, "kind": self.kind}


def validate(items: list[Attachment]) -> list[dict[str, Any]]:
    if len(items) > MAX_FILES:
        raise ValueError(tr("每条消息最多添加 8 个附件"))
    if sum(len(item.raw()) for item in items) > MAX_INPUT_TOTAL:
        raise ValueError(tr("原始附件总大小最多 40 MiB"))
    blocks = [block for item in items for block in item.blocks()]
    if sum(block["type"] == "image" for block in blocks) > 16:
        raise ValueError(tr("每条消息转换后最多 16 张图片，请减少附件或帧数"))
    binary = sum(len(base64.b64decode(b["source"]["data"])) for b in blocks if "source" in b)
    if binary > MAX_TOTAL:
        raise ValueError(tr("转换后的附件总大小最多 10 MiB"))
    return blocks


class Input(str):
    """A draft submission; attachments become message blocks before a turn is logged."""

    attachments: list[Attachment]
    _blocks: list[dict[str, Any]]

    def __new__(cls, text: str, items: list[Attachment]) -> Input:
        value = super().__new__(cls, text)
        value._blocks = validate(items)
        value.attachments = list(items)
        return value

    def blocks(self) -> list[dict[str, Any]]:
        """The agent consumes snapshots; no decoder runs during context assembly."""
        return deepcopy(self._blocks)

    @classmethod
    def from_blocks(cls, message: dict[str, Any]) -> Input:
        """Restore a saved submission without re-reading or re-parsing attachments."""
        content = message["content"]
        value = str.__new__(cls, content[0]["text"])
        value._blocks, value.attachments = deepcopy(content[1:]), []
        return value


def pasted_paths(text: str) -> list[Path]:
    """Recognize an entire pasted file selection, never paths mentioned in a sentence."""
    try:
        # A Finder/drag path with spaces may be escaped, quoted, or a file:// URL.
        exact = Path(text.strip()).expanduser()
        tokens = [text.strip()] if exact.is_file() else shlex.split(text)
        paths = []
        for token in tokens:
            if token.startswith("file://"):
                url = urlparse(token)
                if url.netloc not in ("", "localhost"):
                    return []
                token = unquote(url.path)
            path = Path(token).expanduser()
            if not path.is_file():
                return []
            paths.append(path)
        return paths
    except (ValueError, OSError):
        return []


def clipboard() -> tuple[list[Attachment], str]:
    """Read the user's clipboard only on an explicit paste shortcut; no shell expansion."""

    def run(args: list[str]) -> bytes:
        return subprocess.run(args, capture_output=True, check=True, timeout=3).stdout

    try:
        if sys.platform == "darwin":
            script = """ObjC.import('AppKit');
const p = $.NSPasteboard.generalPasteboard;
const urls = p.readObjectsForClassesOptions($([$.NSURL]), $({}));
if (urls && !urls.isNil() && urls.count > 0) {
  let paths = [];
  for (let i = 0; i < urls.count; i++) paths.push(ObjC.unwrap(urls.objectAtIndex(i).path));
  JSON.stringify({paths});
} else {
  let d = p.dataForType('public.png');
  if (!d || d.isNil()) {
    const t = p.dataForType('public.tiff');
    if (t && !t.isNil()) {
      d = $.NSBitmapImageRep.imageRepWithData(t).representationUsingTypeProperties(4, $({}));
    }
  }
  JSON.stringify(d && !d.isNil() ? {image: ObjC.unwrap(d.base64EncodedStringWithOptions(0))}
                  : {text: ObjC.unwrap(p.stringForType('public.utf8-plain-text')) || ''});
}"""
            result = json.loads(run(["osascript", "-l", "JavaScript", "-e", script]))
            if result.get("paths"):
                paths = pasted_paths("\n".join(shlex.quote(p) for p in result["paths"]))
                if not paths:
                    raise ValueError(tr("剪贴板中的文件已不可用"))
                return [Attachment.read(p) for p in paths], ""
            if result.get("image"):
                return [Attachment(name="剪贴板截图.png", data=result["image"])], ""
            return [], result.get("text", "")
        if shutil.which("wl-paste"):
            types = run(["wl-paste", "--list-types"]).decode().splitlines()
            for mime in ["image/png", "image/jpeg", "image/gif", "image/webp"]:
                if mime in types:
                    data = run(["wl-paste", "--no-newline", "--type", mime])
                    return [Attachment(name="剪贴板截图", data=base64.b64encode(data).decode())], ""
            if "text/uri-list" in types:
                text = run(["wl-paste", "--no-newline", "--type", "text/uri-list"]).decode()
                return [], "\n".join(line for line in text.splitlines() if not line.startswith("#"))
            return [], run(["wl-paste", "--no-newline"]).decode()
        if shutil.which("xclip"):
            targets = run(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]).decode()
            for mime in ["image/png", "image/jpeg", "image/gif", "image/webp"]:
                if mime in targets.splitlines():
                    data = run(["xclip", "-selection", "clipboard", "-o", "-t", mime])
                    return [Attachment(name="剪贴板截图", data=base64.b64encode(data).decode())], ""
            target = next(
                (
                    t
                    for t in ["text/uri-list", "UTF8_STRING", "text/plain"]
                    if t in targets.splitlines()
                ),
                "STRING",
            )
            text = run(["xclip", "-selection", "clipboard", "-o", "-t", target]).decode()
            if target == "text/uri-list":
                text = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
            return [], text
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        raise ValueError(tr("读取剪贴板失败，请粘贴或拖入文件路径")) from e
    raise ValueError(
        tr("当前环境无法读取系统剪贴板；请粘贴文件路径（Linux 可安装 wl-clipboard 或 xclip）")
    )
