"""Small, persistent user preferences; explicit UI choices override legacy defaults."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATUSLINE_ITEMS = {
    "provider": "提供商",
    "model": "模型",
    "session": "对话名称",
    "directory": "工作目录",
    "context": "上下文用量",
    "spent": "累计费用",
    "saved_tokens": "节省 token 估算",
    "saved_cost": "节省费用估算",
}
STATUSLINE_OPTIONS = {
    key: {"full": "完整", "compact": "简约", "off": "关闭"}
    if key == "directory"
    else {"full": "完整", "percent": "百分比", "off": "关闭"}
    if key == "context"
    else {"on": "开", "off": "关"}
    for key in STATUSLINE_ITEMS
}


def path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kaipi/settings.json"


def read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def auto_check_updates() -> bool:
    value = read().get("auto_check_updates")
    return value if isinstance(value, bool) else not bool(os.environ.get("KAIPI_NO_UPDATE_CHECK"))


def set_auto_check_updates(enabled: bool) -> None:
    save({"auto_check_updates": enabled})


def statusline() -> dict[str, str]:
    data = read().get("statusline", {})
    if not isinstance(data, dict):
        data = {}
    values = {}
    for key, options in STATUSLINE_OPTIONS.items():
        default = next(iter(options))
        value = data.get(key, default)
        if isinstance(value, bool):
            value = default if value else "off"  # preserve settings saved by older versions
        values[key] = value if isinstance(value, str) and value in options else default
    return values


def save(changes: dict[str, Any]) -> None:
    data = read() | changes
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
