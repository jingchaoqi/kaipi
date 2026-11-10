from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kaipi import agent, attachments, cli, context, core, graph, history, summarize, views
from kaipi.model import Message, ReferenceEdge
from kaipi.providers import Reply
from tests.conftest import Builder
from tests.test_agent import FakeProvider, call, text


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("explore", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_retry_is_a_sibling_with_only_original_context(
    tmp_path: Path, explore: bool, outcome: str, legacy: bool
) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("retry")
    s._provider = FakeProvider([text("PARENT")])
    parent, _ = core.run_input(s, "parent")
    before = graph.trunk(s.log.state)
    events: list[core.Event] = []

    class Attempt(FakeProvider):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            if len(self.requests) == 1 and outcome == "failed":
                raise TimeoutError("FAILED-ERROR")
            return super().complete(system, messages, points)

    provider = Attempt([call("old", "printf FAILED-TOOL"), text("OLD-ANSWER")])
    s._provider = provider

    def emit(ev: core.Event) -> None:
        events.append(ev)
        if ev.kind == "out" and outcome == "cancelled":
            s.stop()

    if outcome == "completed":
        core.run_input(s, "ORIGINAL", emit, explore=explore)
    else:
        with pytest.raises(agent.Failed if outcome == "failed" else agent.Interrupted):
            core.run_input(s, "ORIGINAL", emit, explore=explore)
        assert events[-1].retryable
    original = next(reversed(s.log.state.nodes.values()))
    original_payload = original.model_dump()
    first_request = provider.requests[0][0]
    if legacy:
        rows = [json.loads(row) for row in s.log.path.read_text().splitlines()]
        for row in rows:
            if row["type"] == "node_created":
                row.pop("request", None)
            if row["type"] == "node_aborted":
                row.pop("grafts", None)
        s.log.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        original_payload["request"] = None
    s = core.Session(tmp_path, resume=s.log.path.stem)
    assert s.can_retry
    replacement = FakeProvider([text("NEW-ANSWER"), text("AGAIN"), text("NEXT")])
    s._provider = replacement
    first, _ = s.retry()
    assert replacement.requests[0][0] == first_request
    assert s.log.state.nodes[first].parent_id == parent
    assert s.log.state.nodes[original.id].model_dump() == original_payload
    assert graph.trunk(s.log.state) == (before if explore else first)
    assert s.leaf == first and s.can_retry
    second, _ = s.retry()
    assert s.log.state.nodes[second].parent_id == parent
    assert replacement.requests[1][0] == first_request
    following, _ = core.run_input(s, "continue")
    assert s.log.state.nodes[following].parent_id == second
    sent = str(replacement.requests[2][0])
    assert "AGAIN" in sent and "OLD-ANSWER" not in sent and "FAILED-TOOL" not in sent


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_retry_restores_frozen_attachments_and_grafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("snapshots")
    b = Builder(s.log)
    parent, side = b.node(None, "parent"), b.node(None, "side", answer="FROZEN-GRAFT")
    s.go(parent)
    s.stage([ReferenceEdge(src_id=side, dst_id="", depth="leaf+summary")])
    s._provider = s._cheap = FakeProvider([text("summary")])
    source = tmp_path / "input.txt"
    source.write_text("FROZEN-ATTACHMENT")
    question = attachments.Input("read [file1]", [attachments.Attachment.read(source)])

    class Attempt(FakeProvider):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            reply = super().complete(system, messages, points)
            if outcome == "failed":
                raise ConnectionError("offline")
            return reply

    provider = Attempt([text("old")])
    s._provider = provider
    if outcome == "failed":
        with pytest.raises(agent.Failed):
            core.run_input(s, question)
    else:
        core.run_input(s, question)
    expected = provider.requests[0][0]
    source.unlink()
    s = core.Session(tmp_path)
    replacement = FakeProvider([text("new")])
    s._provider = replacement

    def forbidden(*args: object) -> None:
        raise AssertionError("retry must reuse snapshots")

    monkeypatch.setattr(attachments, "parse", forbidden)
    monkeypatch.setattr(summarize, "ensure_summary", forbidden)
    nid, _ = s.retry()
    assert replacement.requests[0][0] == expected
    assert "FROZEN-ATTACHMENT" in str(expected) and "FROZEN-GRAFT" in str(expected)
    assert not s.pending and s.log.state.nodes[nid].grafts


def test_preparation_failure_preserves_the_new_input_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("prepare")
    s._provider = FakeProvider([text("old answer")])
    parent, _ = core.run_input(s, "old input")
    s._provider = None

    def fail(model: str) -> FakeProvider:
        raise ValueError("no credentials")

    monkeypatch.setattr(s, "_build", fail)
    with pytest.raises(agent.Failed, match="no credentials"):
        core.run_input(s, "NEW INPUT")
    s = core.Session(tmp_path)
    assert s.can_retry
    s._provider = provider = FakeProvider([text("recovered")])
    nid, _ = s.retry()
    assert s.log.state.nodes[nid].parent_id == parent
    assert provider.requests[0][0][-1]["content"][0]["text"] == "NEW INPUT"


def test_retry_validation_and_async_reservation(tmp_path: Path) -> None:
    s = core.Session(tmp_path, new=True)
    with pytest.raises(ValueError, match="原始请求"):
        s.retry()
    s.rename("async")
    s._provider = FakeProvider([text("root"), text("latest")])
    parent, _ = core.run_input(s, "root")
    core.run_input(s, "latest")
    s.go(parent)
    with pytest.raises(ValueError, match="已变化"):
        s.retry()
    latest = next(reversed(s.log.state.nodes))
    s.go(latest)
    entered, release = threading.Event(), threading.Event()

    class Blocked(FakeProvider):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            entered.set()
            assert release.wait(5)
            return super().complete(system, messages, points)

    s._provider = Blocked([text("new")])
    events: list[core.Event] = []
    worker = s.start_retry(events.append)
    try:
        assert s.busy and entered.wait(5)
        with pytest.raises(core.BusyError):
            s.retry()
        with pytest.raises(core.BusyError):
            s.new()
        s.stop()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not s.busy


def test_retry_hints_and_plain_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("cli")
    s._provider = FakeProvider([text("answer"), text("new answer")])
    cli.cli_turn(s, "question")
    assert "/retry" not in capsys.readouterr().out
    assert cli.slash(s, ["retry"])
    assert context.final_answer(s.log.state.nodes[s.leaf or ""]) == "new answer"
    capsys.readouterr()
    cli.after_failure(s, agent.Failed("offline"))
    assert "/retry" in capsys.readouterr().out
    cli.after_interrupt(s, agent.Interrupted(s.leaf or "", s.log.state.nodes[s.leaf or ""].usage))
    assert "/retry" in capsys.readouterr().out
    with pytest.raises(ValueError, match="不带参数"):
        cli.slash(s, ["retry", "N1"])
    assert any("/retry" in row for row in views.cmd_help(s))
    shown: list[str] = []
    history.replay(s, lambda _, t: shown.append(t), lambda _, t: shown.append(t))
    assert "new answer" in shown and "answer" not in shown


def test_failed_trunk_retry_keeps_old_trunk_and_can_be_retried_again(tmp_path: Path) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("trunk")
    s._provider = FakeProvider([text("PARENT"), text("OLD ANSWER")])
    parent, _ = core.run_input(s, "parent")
    old, _ = core.run_input(s, "QUESTION")

    class Offline(FakeProvider):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            raise ConnectionError("offline")

    s._provider = Offline([])
    with pytest.raises(agent.Failed):
        s.retry()
    assert graph.trunk(s.log.state) == old and s.leaf == parent and s.can_retry
    s._provider = provider = FakeProvider([text("NEW ANSWER")])
    new, _ = s.retry()
    assert graph.trunk(s.log.state) == new and s.log.state.nodes[new].parent_id == parent
    assert "OLD ANSWER" not in str(provider.requests[0][0])
    assert provider.requests[0][0][-1]["content"][0]["text"] == "QUESTION"


@pytest.mark.parametrize("change", ["archive", "pending", "trunk"])
def test_changed_session_rejects_retry_without_side_effects(tmp_path: Path, change: str) -> None:
    s = core.Session(tmp_path, new=True)
    s.rename("changed")
    s._provider = FakeProvider([text("parent"), text("latest")])
    parent, _ = core.run_input(s, "parent")
    latest, _ = core.run_input(s, "latest")
    side = Builder(s.log).node(parent, "side")
    # Restore latest as the newest submitted attempt after the fixture's extra branch.
    s.go(latest)
    s._provider = FakeProvider([text("newest")])
    core.run_input(s, "newest")
    if change == "archive":
        s.set_status(s.leaf or "", restore=False)
    elif change == "pending":
        s.stage([ReferenceEdge(src_id=side, dst_id="", depth="leaf")])
    else:
        s.pin(parent)
    before = s.log.path.read_bytes(), s.cursor_path.read_bytes(), s.leaf, list(s.pending)
    with pytest.raises(ValueError, match="已变化"):
        s.retry()
    assert (s.log.path.read_bytes(), s.cursor_path.read_bytes(), s.leaf, s.pending) == before
    assert not s.busy


def test_legacy_session_restores_input_without_rewriting_history(tmp_path: Path) -> None:
    s = core.Session(tmp_path, new=True)
    node = Builder(s.log).node(None, "legacy")
    s.go(node)
    s = core.Session(tmp_path)
    assert context.user_input(s.log.state.nodes[node]) == "legacy"
    before = s.log.path.read_bytes()
    s._provider = provider = FakeProvider([text("new legacy answer")])
    new, _ = s.retry()
    assert s.log.path.read_bytes().startswith(before)
    assert s.log.state.nodes[new].parent_id is None
    assert provider.requests[0][0][-1]["content"][0]["text"] == "legacy"
