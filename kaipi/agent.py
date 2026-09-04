"""The bash-only agent loop (§5). One user input -> one Node; every tool call in between
belongs to that node."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kaipi import context, guard
from kaipi.model import (
    EdgeAdded,
    Message,
    NodeCompleted,
    NodeCreated,
    ReferenceEdge,
    Usage,
    new_id,
)
from kaipi.providers import Provider
from kaipi.store import Log

MAX_TOOL_OUTPUT = 16_000  # ~4k tokens; the only automatic compression kaipi does
MAX_STEPS = 30
Hook = Callable[[str, str], None]  # (kind, text): "text" | "cmd" | "out" | "guard" | "stop"


def truncate(s: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n[kaipi: {len(s) - limit} chars omitted]\n{s[-half:]}"


def run_bash(cmd: str, cwd: Path, timeout: int = 300) -> str:
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        out = r.stdout + (("\n" + r.stderr) if r.stderr else "")
        if r.returncode:
            out += f"\n[exit {r.returncode}]"
    except subprocess.TimeoutExpired:
        out = f"[kaipi: command timed out after {timeout}s]"
    return truncate(out or "(no output)")


def run_turn(
    log: Log,
    provider: Provider,
    leaf_id: str | None,
    edges: list[ReferenceEdge],
    text: str,
    *,
    exploration: bool,
    cwd: Path,
    hook: Hook = lambda _k, _t: None,
    max_steps: int = MAX_STEPS,
) -> str:
    """Create, run and freeze one node under leaf_id. Returns the new node id."""
    state = log.state
    ctx = context.build(state, leaf_id, edges, text, guard=exploration)
    node_id = new_id()
    log.append(NodeCreated(id=node_id, parent_id=leaf_id, model=provider.model))
    for e in edges:
        log.append(EdgeAdded(edge=e.model_copy(update={"dst_id": node_id})))

    messages: list[Message] = list(ctx.messages)
    usage, last, dropped, dirty = Usage(), Usage(), 0, False
    tree0 = guard.snapshot(cwd)
    baseline = (tree0, guard.head(cwd)) if exploration and tree0 else None
    for _ in range(max_steps):
        reply = provider.complete(ctx.system, messages, ctx.cache_points)
        usage, last, dropped = usage + reply.usage, reply.usage, dropped + reply.dropped_thinking
        messages.append({"role": "assistant", "content": reply.content})
        for b in reply.content:
            if b.get("type") == "text" and b["text"].strip():
                hook("text", b["text"])
        calls = [b for b in reply.content if b.get("type") == "tool_use"]
        if reply.stop_reason != "tool_use" or not calls:
            if reply.stop_reason in ("max_tokens", "refusal"):
                hook("stop", reply.stop_reason)
            break
        results: list[dict[str, Any]] = []
        for call in calls:
            cmd = str(call["input"].get("command", ""))
            hook("cmd", cmd)
            out = run_bash(cmd, cwd)
            hook("out", out)
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
        user: Message = {"role": "user", "content": results}
        if baseline is not None and (guard.snapshot(cwd), guard.head(cwd)) != baseline:
            user["content"].append({"type": "text", "text": guard.WARNING})
            hook("guard", "working tree is dirty")
            dirty, baseline = True, None  # warn once per turn
        messages.append(user)
    else:
        hook("stop", "max_steps")

    tree1 = guard.snapshot(cwd)
    if tree1:
        guard.keep(cwd, f"{log.state.session_id}/{node_id}", tree1)
    log.append(
        NodeCompleted(
            id=node_id,
            payload=messages[ctx.payload_start :],
            grafts=ctx.grafts,
            usage=usage,
            context_tokens=last.context + last.output,
            dropped_thinking=dropped,
            tree=tree1,
            paths=guard.changed(cwd, tree0, tree1),
            guard_dirty=dirty,
        )
    )
    return node_id
