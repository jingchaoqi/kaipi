"""The product's interfaces are adapters, never alternative session implementations."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from kaipi import agent, context, core, server, summarize, views
from kaipi.model import Message, Node, NodeAborted, NodeCreated, ReferenceEdge, Usage
from kaipi.providers import Reply
from kaipi.store import Log
from tests.conftest import Builder
from tests.test_cli import Echo


def test_core_runs_without_importing_any_interface(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path
from kaipi import core
s = core.Session(Path(sys.argv[1]), new=True)
s.rename('headless session')
assert core.Session(s.cwd, resume=s.log.path.stem).log.state.name == 'headless session'
assert not {'kaipi.cli', 'kaipi.tui', 'kaipi.server', 'kaipi.views', 'kaipi.configuration',
            'typer', 'prompt_toolkit'} & sys.modules.keys()
""",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    # Canvas must not reach through the CLI even through a local import.
    tree = ast.parse(Path(server.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "kaipi.cli"
            assert not (node.module == "kaipi" and any(n.name == "cli" for n in node.names))


def test_core_cli_and_canvas_apply_the_same_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core.Session, "_build", lambda *_: Echo())
    results = []
    for surface in ("core", "cli", "canvas"):
        s = core.Session(tmp_path / surface, new=True)
        b = Builder(s.log)
        root = b.node(None, "root")
        s.leaf = b.node(root, "second")
        s.save()
        previous = s.log.path
        if surface == "core":
            s.go("N1")
            s.graft("N2", "leaf", [])
            s.rename("shared conversation")
        elif surface == "cli":
            views.cmd_go(s, "N1")
            views.cmd_graft(s, "N2", "leaf", [])
            views.cmd_rename(s, "shared conversation")
        else:
            canvas = server.Canvas(s, server.Hub())
            for command in ("/go N1", "/graft N2 --depth leaf", "/rename shared conversation"):
                assert not canvas.command(command).startswith("错误：")
            canvas.request_handoff()
        restored = core.Session(s.cwd, resume=previous.stem)
        results.append(
            (
                restored.leaf,
                restored.pending,
                restored.log.state.name,
                restored.log.state.trunk_pin,
                restored.inspect_context("next"),
            )
        )
        before = previous.read_bytes()
        s.new()
        s.resume(previous.stem)
        assert s.log.path == previous and previous.read_bytes() == before
    assert results[0] == results[1] == results[2]


def test_core_owns_busy_stop_and_persistence(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    class Blocked(Echo):
        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            entered.set()
            assert release.wait(5)
            return Reply(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "bash",
                        "input": {"command": "touch must-not-run"},
                    }
                ],
                stop_reason="tool_use",
                usage=Usage(input_uncached=100, output=5),
            )

    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    s.leaf = root
    side = b.node(root, "side finding")
    s.pin(root)
    s.stage([ReferenceEdge(src_id=side, dst_id="", depth="leaf")])
    s._provider = s._cheap = Blocked()
    events: list[core.Event] = []
    finished: list[bool] = []

    def emit(ev: core.Event) -> None:
        events.append(ev)
        if ev.kind == "aborted":
            finished.append(s.busy and Log(s.log.path).state.nodes[ev.node_id].status == "aborted")

    worker = s.start("work", emit)
    try:
        assert entered.wait(5), events
        before = s.log.path.read_bytes(), s.cursor_path.read_bytes()
        for action in (
            lambda: s.go("N1"),
            s.new,
            lambda: s.stage([]),
            lambda: s.rename("racing name"),
            lambda: core.run_input(s, "racing turn"),
        ):
            with pytest.raises(core.BusyError):
                action()
        assert before == (s.log.path.read_bytes(), s.cursor_path.read_bytes())
        s.stop()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and finished == [True]
    assert not (tmp_path / "must-not-run").exists()
    restored = core.Session(tmp_path, resume=s.log.path.stem)
    assert restored.leaf == root and restored.pending == s.pending and s.pending
    assert any(ev.kind == "aborted" for ev in events)
    assert all("\033" not in ev.text for ev in events)


def test_failure_before_node_creation_keeps_pending_grafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    side = b.node(root, "side")
    s.leaf = root
    edges = [ReferenceEdge(src_id=side, dst_id="", depth="leaf")]
    s.stage(edges)

    def fail(*_: object) -> Echo:
        raise ValueError("missing provider configuration")

    monkeypatch.setattr(s, "_build", fail)
    with pytest.raises(agent.Failed, match="missing provider"):
        core.run_input(s, "continue")
    restored = core.Session(tmp_path, resume=s.log.path.stem)
    assert not s.busy and restored.pending == edges and restored.leaf == root


def test_completed_event_is_emitted_after_save(tmp_path: Path) -> None:
    s = core.Session(tmp_path, new=True)
    s._provider = s._cheap = Echo()
    seen = []

    def emit(ev: core.Event) -> None:
        if ev.kind == "completed":
            resumed = core.Session(tmp_path, resume=s.log.path.stem)
            seen.append((resumed.leaf, s.busy, resumed.log.state.nodes[ev.node_id].completed))

    node, _ = core.run_input(s, "hello", emit)
    assert seen == [(node, True, True)]
    assert not s.busy


@pytest.mark.parametrize("outcome", ["completed", "aborted", "error"])
def test_final_event_precedes_next_turn_and_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("event ordering")
    s._provider = s._cheap = Echo()
    if outcome == "error":
        s._provider = None
        monkeypatch.setattr(s, "_build", Mock(side_effect=ValueError("missing provider")))
    canvas = server.Canvas(s, server.Hub())
    entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
    events: list[str] = []

    expected = "aborted" if outcome == "error" else outcome

    def emit(ev: core.Event) -> None:
        if ev.kind == "start" and outcome == "aborted":
            s.stop()
        if ev.kind == expected:
            entered.set()
            assert release.wait(5)
            # Reentrant callbacks may read the result, but cannot change the session.
            with pytest.raises(core.BusyError):
                s.new()
            with pytest.raises(core.BusyError):
                canvas.request_handoff()
            events.append("first:" + ev.kind)

    def next_turn() -> None:
        attempting.set()
        core.run_input(s, "second", lambda ev: events.append("second:" + ev.kind))

    first = s.start("first", emit)
    second = threading.Thread(target=next_turn)
    try:
        assert entered.wait(5)
        assert s.busy and not s.idle.is_set()
        s._provider = Echo()
        second.start()
        assert attempting.wait(5)
        assert not events and not canvas.handoff
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert events.index("first:" + expected) < events.index("second:start")
    assert events[-1] == "second:completed" and not s.busy
    canvas.request_handoff()
    assert canvas.handoff


def test_staged_aliases_survive_reload_and_export(tmp_path: Path) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    side = b.node(root, "side")
    s.go(root)
    edge = ReferenceEdge(src_id="N2", dst_id="", depth="leaf")
    s.stage([edge])
    assert edge.src_id == "N2"  # do not rewrite the caller's object
    saved = s.cursor_path.read_bytes()
    with pytest.raises(ValueError, match="missing"):
        s.stage([edge, ReferenceEdge(src_id="missing", dst_id="")])
    assert s.cursor_path.read_bytes() == saved and s.pending[0].src_id == side
    restored = core.Session(tmp_path, resume=s.log.path.stem)
    assert restored.pending[0].src_id == side
    assert restored.inspect_context("next") == s.inspect_context("next")
    assert "side" in restored.export_context("next").read_text()


def test_cursor_operations_save_once_and_queries_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    side = b.node(root, "side")
    s.go(root)
    previous = s.log.path.stem
    save = Mock(wraps=s.save)
    monkeypatch.setattr(s, "save", save)
    s._provider = Echo()
    for action in (
        lambda: s.go(root),
        lambda: s.graft(side, "leaf", []),
        lambda: s.stage([]),
        lambda: s.set_status(side, restore=False),
        lambda: s.set_status(side, restore=True),
        lambda: s.pin(root),
        lambda: s.rewind(root, "conversation"),
        s.new,
        lambda: s.resume(previous),
    ):
        save.reset_mock()
        action()
        save.assert_called_once_with()
    save.reset_mock()
    s.rename("renamed")
    s.resume(previous)  # already here
    s.export_context("next")
    s.refresh_configuration()
    s.state()
    save.assert_not_called()
    assert core.Session(tmp_path).log.state.name == "renamed"


def test_state_keeps_one_snapshot_while_a_turn_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = core.Session(tmp_path, new=True)
    root = Builder(s.log).node(None, "root")
    s.go(root)
    s._provider = s._cheap = Echo()
    started, proceed, reading, release = [threading.Event() for _ in range(4)]
    original = context.user_input

    def emit(ev: core.Event) -> None:
        if ev.kind == "start":
            started.set()
            assert proceed.wait(5)

    def read_input(node: Node) -> str:
        if threading.current_thread().name == "snapshot-reader":
            reading.set()
            assert release.wait(5)
        return original(node)

    monkeypatch.setattr(context, "user_input", read_input)
    snapshots = []
    reader = threading.Thread(target=lambda: snapshots.append(s.state()), name="snapshot-reader")
    worker = s.start("next", emit)
    try:
        assert started.wait(5)
        reader.start()
        assert reading.wait(5)
        proceed.set()
        worker.join(5)
        assert not worker.is_alive()  # rendering the old snapshot must not hold the turn lock
    finally:
        proceed.set()
        release.set()
        worker.join(5)
        if reader.ident is not None:
            reader.join(5)
    snapshot = snapshots[0]
    assert snapshot["leaf"] == root
    assert {n["id"] for n in snapshot["nodes"]} == {root}
    assert s.leaf != root and len(s.state()["nodes"]) == 2


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_summary_preparation_failure_preserves_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root, side = b.node(None, "root"), b.node(None, "side")
    s.go(root)
    edges = [ReferenceEdge(src_id=side, dst_id="", depth="leaf+summary")]
    s.stage(edges)
    s._provider = s._cheap = Echo()
    original_summary = summarize.ensure_summary
    monkeypatch.setattr(summarize, "ensure_summary", Mock(side_effect=failure("interrupted")))

    def emit(ev: core.Event) -> None:
        if ev.kind == "summary_failed":
            raise RuntimeError("client callback failed before the turn")

    with pytest.raises(agent.Interrupted if failure is KeyboardInterrupt else agent.Failed):
        core.run_input(s, "next", emit)
    restored = core.Session(tmp_path)
    assert s.pending == restored.pending == edges and not s.busy
    assert s.leaf == restored.leaf == root and len(restored.log.state.nodes) == 3
    assert restored.can_retry
    monkeypatch.setattr(summarize, "ensure_summary", original_summary)
    core.run_input(s, "retry")
    assert s.pending == [] and s.log.state.nodes[s.leaf or ""].grafts[0].src_id == side


@pytest.mark.parametrize("operation", ["go", "stage", "new", "resume"])
@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_cursor_write_failure_keeps_previous_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure_point: str
) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root, side = b.node(None, "root"), b.node(None, "side")
    s.go(root)
    previous = s.log.path.stem
    s.new()
    other = s.log.path.stem
    s.resume(previous)
    s.go(root)
    before = s.log, s.leaf, s.pending, s.cursor_path.read_bytes()
    monkeypatch.setattr(os, failure_point, Mock(side_effect=OSError("write failed")))
    actions: dict[str, Callable[[], object]] = {
        "go": lambda: s.go(side),
        "stage": lambda: s.stage([ReferenceEdge(src_id=side, dst_id="", depth="leaf")]),
        "new": s.new,
        "resume": lambda: s.resume(other),
    }
    with pytest.raises(OSError, match="write failed"):
        actions[operation]()
    assert (s.log, s.leaf, s.pending, s.cursor_path.read_bytes()) == before
    assert not list(s.cursor_path.parent.glob(".cursor-*"))
    assert core.Session(tmp_path).leaf == root


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("broken", [b'{"session":', b"\xff"])
def test_corrupt_cursor_does_not_block_history_recovery(
    tmp_path: Path, resume: bool, broken: bytes
) -> None:
    s = core.Session(tmp_path, new=True)
    root = Builder(s.log).node(None, "root")
    s.go(root)
    history = s.log.path.read_bytes()
    s.cursor_path.write_bytes(broken)
    restored = core.Session(tmp_path, resume=s.log.path.stem if resume else None)
    assert restored.leaf == root and restored.log.path.read_bytes() == history
    backups = list(s.cursor_path.parent.glob("cursor.corrupt-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == broken
    restored.save()
    assert core.Session(tmp_path).leaf == root and backups[0].read_bytes() == broken


@pytest.mark.parametrize("target", ["ancestor", "aborted", "tombstone"])
def test_stage_and_graft_reject_the_same_invalid_nodes(tmp_path: Path, target: str) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    side = b.node(root, "side")
    s.go(root)
    s._provider = Echo()
    invalid = root
    if target != "ancestor":
        invalid = "invalid"
        s.log.append(NodeCreated(id=invalid, parent_id=root, model="fake"))
        if target == "aborted":
            s.log.append(NodeAborted(id=invalid, payload=[], usage=Usage()))
    valid = ReferenceEdge(src_id=side, dst_id="", depth="leaf")
    s.stage([valid])
    before = s.cursor_path.read_bytes()
    with pytest.raises(ValueError) as graft_error:
        s.graft(invalid, "leaf", [])
    with pytest.raises(ValueError) as stage_error:
        s.stage([valid, ReferenceEdge(src_id=invalid, dst_id="", depth="leaf")])
    assert str(graft_error.value) == str(stage_error.value)
    assert s.pending == [valid] and s.cursor_path.read_bytes() == before


def test_waiting_canvas_turn_is_rejected_after_handoff(tmp_path: Path) -> None:
    s = core.Session(tmp_path, new=True)
    canvas = server.Canvas(s, server.Hub())
    attempting = threading.Event()
    rejected: list[str] = []

    def submit() -> None:
        attempting.set()
        try:
            canvas.turn("late request")
        except server.ClosedError:
            rejected.append("closed")

    with s.lock:
        worker = threading.Thread(target=submit)
        worker.start()
        assert attempting.wait(5)
        canvas.request_handoff()
    worker.join(5)
    assert not worker.is_alive() and rejected == ["closed"]
    assert not s.busy and not s.log.state.nodes
    with pytest.raises(server.ClosedError):
        canvas.command("/rename late request")


def test_failed_cursor_save_does_not_reactivate_an_archived_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = core.Session(tmp_path, new=True)
    root = Builder(s.log).node(None, "root")
    s.go(root)
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("write failed")))
    with pytest.raises(OSError):
        s.set_status(root, restore=False)
    assert s.log.state.nodes[root].status == "archived"
    assert s.leaf is None and core.Session(tmp_path).leaf is None


@pytest.mark.parametrize("operation", ["go", "pin", "stage", "new", "resume", "rewind"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_cursor_commit_matches_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error: type[BaseException],
    committed: bool,
) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    side = b.node(root, "side")
    s.go(root)
    previous = s.log.path.stem
    s.new()
    other = s.log.path.stem
    s.resume(previous)
    s.go(root)
    before = s.cursor_path.read_bytes()
    replace = os.replace

    def interrupted(src: str, dst: Path) -> None:
        if committed:
            replace(src, dst)
        raise error("interrupted commit")

    monkeypatch.setattr(os, "replace", interrupted)
    actions: dict[str, Callable[[], object]] = {
        "go": lambda: s.go(side),
        "pin": lambda: s.pin(side),
        "stage": lambda: s.stage([ReferenceEdge(src_id=side, dst_id="", depth="leaf")]),
        "new": s.new,
        "resume": lambda: s.resume(other),
        "rewind": lambda: s.rewind(side, "conversation"),
    }
    with pytest.raises(error, match="interrupted commit"):
        actions[operation]()
    restored = core.Session(tmp_path)
    assert (s.log.path, s.leaf, s.pending) == (restored.log.path, restored.leaf, restored.pending)
    assert (s.cursor_path.read_bytes() != before) == committed
    assert not list(s.cursor_path.parent.glob(".cursor-*"))


@pytest.mark.parametrize("operation", ["go", "pin", "rewind", "archive", "restore"])
def test_navigation_refuses_graft_conflicts_before_any_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    s = core.Session(tmp_path, new=True)
    b = Builder(s.log)
    root = b.node(None, "root")
    side = b.node(None, "side")
    s.go(root)
    if operation == "restore":
        s.set_status(root, restore=False)
        s.set_status(side, restore=False)
    s._provider = s._cheap = Echo()
    s.graft(side, "leaf", [])
    before = s.log.path.read_bytes(), s.cursor_path.read_bytes(), s.leaf, list(s.pending)
    rewind_code = Mock(side_effect=AssertionError("must validate before restoring code"))
    monkeypatch.setattr(core, "rewind_code", rewind_code)
    actions: dict[str, Callable[[], object]] = {
        "go": lambda: s.go(side),
        "pin": lambda: s.pin(side),
        "rewind": lambda: s.rewind(side, "both"),
        "archive": lambda: s.set_status(root, restore=False),
        "restore": lambda: s.set_status(side, restore=True),
    }
    with pytest.raises(ValueError, match="already in this lineage"):
        actions[operation]()
    assert (s.log.path.read_bytes(), s.cursor_path.read_bytes(), s.leaf, s.pending) == before
    rewind_code.assert_not_called()
    core.run_input(s, "continue")
    assert not s.pending and s.log.state.nodes[s.leaf or ""].grafts[0].src_id == side
