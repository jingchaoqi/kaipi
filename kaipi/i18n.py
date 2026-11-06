"""UI-only translations. Never translate prompts, stored messages or tool output."""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import cast

from kaipi import settings

LANGUAGES = {"zh": "简体中文", "en": "English"}


@lru_cache(maxsize=1)
def messages() -> dict[str, dict[str, str]]:
    return cast(
        dict[str, dict[str, str]],
        json.loads(
            Path(__file__)
            .with_name("locales")
            .joinpath("messages.json")
            .read_text(encoding="utf-8")
        ),
    )


@lru_cache(maxsize=1)
def _language(path: Path, modified: int, inode: int) -> str:
    value = settings.read().get("language")
    return value if isinstance(value, str) and value in LANGUAGES else "zh"


def language() -> str:
    path = settings.path()
    try:
        stat = path.stat()
    except OSError:
        return "zh"
    return _language(path, stat.st_mtime_ns, stat.st_ino)


def tr(message: str, **values: object) -> str:
    """Source strings are message IDs (gettext style); substitutions stay untouched."""
    translated = messages().get(message, {}).get(language(), message)
    return translated.format(**values) if values else translated


def canvas_page(source: str) -> bytes:
    """Expand only marked static UI copy; session content never passes through here."""

    def replace(match: re.Match[str]) -> str:
        text = tr(match[2])
        if match[1] == "html":
            return html.escape(text, quote=True)
        return (
            json.dumps(text, ensure_ascii=False)[1:-1]
            .replace("'", "\\u0027")
            .replace("`", "\\u0060")
            .replace("$", "\\u0024")
            .replace("<", "\\u003c")
        )

    return re.sub(r"\{\{(html|js):(.+?)\}\}", replace, source).encode()


def attachment_summary(text: str) -> str:
    """Localize generated draft metadata, leaving the original parser payload intact."""
    text = tr(text)
    text = re.sub(r" · (\d+) 帧全部保留", lambda m: tr(" · {count} 帧全部保留", count=m[1]), text)
    text = text.replace(" · 帧时长(ms): ", tr(" · 帧时长(ms): "))
    return re.sub(
        r" · 已缩放，长边 (\d+)px 以内",
        lambda m: tr(" · 已缩放，长边 {edge}px 以内", edge=m[1]),
        text,
    )
