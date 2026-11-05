"""A local Markdown view of the next turn's assembled context."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kaipi import context, graph

if TYPE_CHECKING:
    from kaipi.cli import Session


def export(s: Session, placeholder: str) -> Path:
    state = s.log.state
    ctx = context.build(
        state, s.leaf, s.pending, placeholder, guard=graph.is_exploration(state, s.leaf)
    )
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", state.name or "未命名").strip(" .")
    title = title.encode("utf-8")[:120].decode("utf-8", errors="ignore") or "未命名"
    stem = f"{datetime.now():%Y%m%d%H%M%S}-{title}-{s.name(s.leaf)}-context"
    index = 1
    while True:
        path = s.cwd / f"{stem}{f'-{index}' if index > 1 else ''}.md"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            index += 1
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"system": ctx.system, "messages": ctx.messages},
                stream,
                ensure_ascii=False,
                indent=2,
            )
            stream.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.resolve()
