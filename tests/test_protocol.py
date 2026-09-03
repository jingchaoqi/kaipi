"""Behaviour that only shows up when the real provider code talks to a real endpoint.

These run against scripts/mockapi.py, which validates request shapes strictly and implements
a byte-keyed prefix cache with the documented 20-position lookback.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mockapi  # noqa: E402
import smoke  # noqa: E402

from kaipi import cli  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env() -> Iterator[None]:
    """These tests point provider presets at the mock through <PROVIDER>_BASE_URL; that must
    not leak into the tests that check the real defaults."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def endpoint() -> Iterator[tuple[mockapi.MockServer, str]]:
    srv, url = mockapi.start()
    yield srv, url
    srv.shutdown()


def session_on(url: str, model: str, provider_env: str, path: str = "/v1") -> tuple[Any, Any, Path]:
    root = Path(tempfile.mkdtemp(prefix="kaipi-proto-"))
    smoke.make_repo(root)
    os.environ[f"{provider_env}_BASE_URL"] = url + path
    os.environ[f"{provider_env}_API_KEY"] = "mock-key"
    os.environ["KAIPI_MODEL"] = model
    s = cli.Session(root, new=True)
    tally = smoke.Tally(s._build(model))
    s._provider = tally
    s._cheap = tally
    return s, tally, root


def test_a_long_tool_loop_does_not_break_the_next_turn_cache(endpoint) -> None:  # type: ignore[no-untyped-def]
    """A turn with many sequential bash calls adds far more than 20 cache positions. It stays
    safe because kaipi always marks the lineage end, which sits one position after the
    previous turn's last request - inside the lookback window however long the loop was."""
    srv, url = endpoint

    class LoopBrain(mockapi.Brain):
        def act(self, turn: mockapi.Turn) -> dict[str, Any]:
            n = len(turn.commands_this_turn)
            if "LOOP" in turn.last_user_text and n < 14:
                return {"command": f"echo step {n}"}
            if n < 1:
                return {"command": "echo hi"}
            return {"text": f"done after {n} commands"}

    srv.api.brain = LoopBrain()
    s, tally, root = session_on(url, "claude-opus-5", "ANTHROPIC", path="")
    os.chdir(root)

    reads: list[tuple[int, int]] = []
    prev_ctx = 0
    for prompt in ["first", "LOOP many steps", "after the loop"]:
        first = len(tally.replies)
        cli.run_input(s, prompt, lambda k, t: None)
        reads.append((tally.replies[first].usage.cache_read, prev_ctx))
        prev_ctx = s.log.state.nodes[s.leaf or ""].context_tokens

    loop_requests = len(tally.turns) if tally.turns else 0
    assert loop_requests >= 0  # (turns is only populated by smoke.turn; not used here)
    read_after_loop, ctx_of_loop_turn = reads[2]
    assert ctx_of_loop_turn > 500, "the loop turn should have built a large context"
    # everything except the final assistant message, which no earlier request ever carried
    assert read_after_loop >= ctx_of_loop_turn * 0.9, (
        f"only {read_after_loop} of {ctx_of_loop_turn} was read back: the long loop pushed the "
        "previous turn out of the cache lookback window"
    )


@pytest.mark.parametrize(
    ("model", "env", "path"),
    [
        ("kimi-anthropic/kimi-k3[1m]", "KIMI_ANTHROPIC", ""),
        ("glm-anthropic/glm-5.2[1m]", "GLM_ANTHROPIC", ""),
        ("deepseek-anthropic/deepseek-v4-pro", "DEEPSEEK_ANTHROPIC", ""),
        ("opencode-anthropic/claude-opus-5", "OPENCODE_ANTHROPIC", ""),
    ],
)
def test_anthropic_compatible_gateways(endpoint, model: str, env: str, path: str) -> None:  # type: ignore[no-untyped-def]
    """The `*-anthropic` presets must send kaipi's cache breakpoints but omit the
    preserved-thinking parameters, which only Anthropic itself understands."""
    srv, url = endpoint
    s, tally, root = session_on(url, model, env, path)
    os.chdir(root)
    cli.run_input(s, "Inspect app.py and tell me the bug.", lambda k, t: None)

    body = srv.api.requests[0]["body"]
    assert "thinking" not in body, "a compat gateway must not be sent thinking.block_binding"
    marked = [
        m
        for m in body["messages"]
        if isinstance(m.get("content"), list) and "cache_control" in m["content"][-1]
    ]
    assert marked, "cache breakpoints are still useful on a compat gateway"
    assert body["tools"] == [{"type": "bash_20250124", "name": "bash"}]
    assert s.log.state.nodes[s.leaf or ""].usage.output > 0


def test_oversized_tool_output_is_truncated_before_it_is_frozen(endpoint) -> None:  # type: ignore[no-untyped-def]
    """The only automatic compression kaipi does. It has to happen before the payload freezes,
    or the node keeps a megabyte of build log forever."""
    srv, url = endpoint

    class NoisyBrain(mockapi.Brain):
        def act(self, turn: mockapi.Turn) -> dict[str, Any]:
            if not turn.commands_this_turn:
                return {"command": "python -c \"print('x' * 200000)\""}
            return {"text": "that was a lot of output"}

    srv.api.brain = NoisyBrain()
    s, tally, root = session_on(url, "claude-opus-5", "ANTHROPIC", path="")
    os.chdir(root)
    cli.run_input(s, "run the noisy thing", lambda k, t: None)

    node = s.log.state.nodes[s.leaf or ""]
    results = [
        b
        for m in node.payload
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    ]
    assert results, "the tool result should be in the frozen payload"
    body = str(results[0]["content"])
    assert len(body) <= 17_000, f"tool output was frozen at {len(body)} chars, untruncated"
    assert "omitted" in body and body.startswith("x" * 100)


def test_a_session_survives_an_interrupted_turn(endpoint) -> None:  # type: ignore[no-untyped-def]
    """Ctrl-C leaves node_created with no node_completed. The fold must tombstone it and the
    trunk must fall back to the last good node, so the next `kaipi` resumes cleanly."""
    srv, url = endpoint
    s, tally, root = session_on(url, "claude-opus-5", "ANTHROPIC", path="")
    os.chdir(root)
    cli.run_input(s, "first, which completes", lambda k, t: None)
    good = s.leaf

    def die(*_: Any, **__: Any) -> None:
        raise KeyboardInterrupt

    tally.inner.complete = die
    with pytest.raises(KeyboardInterrupt):
        cli.run_input(s, "second, which is interrupted", lambda k, t: None)

    from kaipi import graph
    from kaipi.store import Log

    reloaded = Log(s.log.path).state
    incomplete = [n for n in reloaded.nodes.values() if n.status == "tombstone"]
    assert len(incomplete) == 1 and not incomplete[0].completed
    assert graph.trunk(reloaded) == good, "the trunk must fall back to the last completed node"
    resumed = cli.Session(root)
    assert resumed.leaf == good
