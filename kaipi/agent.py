"""The bash-only agent loop (§5). One user input -> one Node; every tool call in between
belongs to that node."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kaipi import context, guard
from kaipi.model import (
    EdgeAdded,
    Message,
    NodeAborted,
    NodeCompleted,
    NodeCreated,
    ReferenceEdge,
    Usage,
    new_id,
)
from kaipi.providers import Provider, RateLimited, Reply
from kaipi.store import Log

MAX_TOOL_OUTPUT = 16_000  # ~4k tokens; the only automatic compression kaipi does
MAX_STEPS = 30
# A rate limit is a normal condition, not a failure: an entry-tier endpoint can allow as
# few as 3 requests a minute, and one turn with six commands is seven requests in as many
# seconds. Wait and retry rather than losing the turn.
RATE_LIMIT_TRIES = 6
RATE_LIMIT_MAX_WAIT = 60.0
Hook = Callable[[str, str], None]  # (kind, text): "text" | "cmd" | "out" | "guard" | "stop"


class Failed(Exception):
    """The turn ended because the provider did, not because the user asked. The node is
    already recorded as aborted - what it spent is on the bill - when this is raised."""


class Interrupted(Exception):
    """The user stopped the turn (Esc in the terminal, the canvas's stop button, Ctrl-C).
    The node is already recorded as aborted when this is raised."""

    def __init__(self, node_id: str, usage: Usage) -> None:
        super().__init__(f"turn {node_id} was interrupted")
        self.node_id, self.usage = node_id, usage


def truncate(s: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n[kaipi: {len(s) - limit} chars omitted]\n{s[-half:]}"


def run_bash(cmd: str, cwd: Path, timeout: int = 300, stop: threading.Event | None = None) -> str:
    """The command runs in its own process group, so a stop kills the whole command rather
    than just the shell: pressing Esc during a 30-second test run has to end it now."""
    p = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline, note = time.monotonic() + timeout, ""
    while True:
        try:
            out, err = p.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                note = "\n[kaipi: stopped by the user]"
            elif time.monotonic() > deadline:
                note = f"\n[kaipi: command timed out after {timeout}s]"
            else:
                continue
            _kill(p)
            out, err = p.communicate()
            break
        except BaseException:
            # Ctrl-C included: the command is in its own process group, so nothing else
            # will ever kill it. Leaving a `npm run dev` or a test suite running forever
            # is not an acceptable way to exit.
            _kill(p)
            p.communicate()
            raise
    text = out + (("\n" + err) if err else "")
    if p.returncode and not note:
        text += f"\n[exit {p.returncode}]"
    return truncate((text or "(no output)") + note)


def _kill(p: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except OSError:
        p.kill()


def _complete(
    provider: Provider,
    ctx: context.Context,
    messages: list[Message],
    hook: Hook,
    stop: threading.Event | None,
) -> Reply:
    """One request, waiting out the vendor's rate limit. The wait is announced (it is the
    difference between "slow" and "hung") and interruptible: Esc during it still stops."""
    for attempt in range(RATE_LIMIT_TRIES):
        try:
            return provider.complete(ctx.system, messages, ctx.cache_points)
        except RateLimited as limit:
            if attempt == RATE_LIMIT_TRIES - 1:
                raise
            wait = min(limit.retry_after or 2 ** (attempt + 1), RATE_LIMIT_MAX_WAIT)
            tries = RATE_LIMIT_TRIES - 1
            hook("wait", f"被限流，{wait:.0f} 秒后重试（第 {attempt + 1}/{tries} 次）")
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if stop is not None and stop.is_set():
                    raise
                time.sleep(0.1)
    raise RuntimeError("unreachable")


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
    stop: threading.Event | None = None,
) -> str:
    """Create, run and freeze one node under leaf_id. Returns the new node id.
    `stop` interrupts the turn at the next step boundary; so does Ctrl-C, at once."""
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

    def abort() -> Interrupted:
        """Freeze what the model produced as an aborted node: kept so the user can see what
        was thrown away, billed because it was really spent, never assembled into a request
        again. The files it changed are recorded like any turn's, or a later rewind would
        not know they exist."""
        tree = guard.snapshot(cwd)
        if tree:
            guard.keep(cwd, f"{log.state.session_id}/{node_id}", tree)
        log.append(
            NodeAborted(
                id=node_id,
                payload=messages[ctx.payload_start :],
                usage=usage,
                tree=tree,
                paths=guard.changed(cwd, tree0, tree),
                guard_dirty=dirty or (baseline is not None and tree != tree0),
            )
        )
        return Interrupted(node_id, usage)

    try:
        for _ in range(max_steps):
            if stop is not None and stop.is_set():
                raise abort()
            reply = _complete(provider, ctx, messages, hook, stop)
            usage = usage + reply.usage
            last, dropped = reply.usage, dropped + reply.dropped_thinking
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
                # Checked per command, not once per step: a stop that arrives while the
                # model is answering must not then run `rm -rf build` because the reply
                # happened to contain it.
                if stop is not None and stop.is_set():
                    raise abort()
                cmd = str(call["input"].get("command", ""))
                hook("cmd", cmd)
                out = run_bash(cmd, cwd, stop=stop)
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
    except Interrupted:  # the user's own stop, already recorded: it is not a failure
        raise
    except KeyboardInterrupt:  # Ctrl-C, which can land in the middle of a model call
        raise abort() from None
    except Exception as e:
        # The vendor refused, the network died, the key expired. The turn is over either
        # way; what must not happen is losing the tokens it already burned and taking the
        # whole session down with a traceback, so it ends as an aborted node like any
        # other and the caller gets a sentence.
        abort()
        raise Failed(f"{type(e).__name__}: {e}".split("\n")[0][:300]) from None

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
