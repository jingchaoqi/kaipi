"""Replay stored conversation content for display, without requests or tool execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from kaipi import context, graph
from kaipi.model import blocks

if TYPE_CHECKING:
    from kaipi.cli import Session


def replay(
    session: Session,
    question: Callable[[str, str], None],
    emit: Callable[[str, str], None],
) -> None:
    if session.leaf is None:
        return
    for node in graph.lineage(session.log.state, session.leaf):
        if not node.payload:
            continue
        question(session.name(node.parent_id), context.user_input(node))
        for message in node.payload:
            if message["role"] != "assistant":
                continue
            for block in blocks(message["content"]):
                if block.get("type") == "text":
                    emit("text", block["text"])
                elif block.get("type") == "tool_use":
                    emit("cmd", str(block.get("input", {}).get("command", "")))
        emit("ledger", session.turn_line(node.id))
