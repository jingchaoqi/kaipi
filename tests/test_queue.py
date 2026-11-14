from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.formatted_text import fragment_list_to_text

from kaipi import attachments, context, core, graph, ledger, tui
from kaipi.model import Message
from kaipi.providers import Reply
from tests.conftest import Builder
from tests.test_cli import Echo
from tests.test_command_ui import terminal


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> core.Session:
    monkeypatch.setenv("TERM", "xterm-256color")
    s = core.Session(tmp_path, new=True)
    s._provider = s._cheap = Echo()
    s.leaf = Builder(s.log).node(None, "root")
    s.pricing.models["fake"] = ledger.Price(input=1, output=2)
    s.save()
    return s


async def until(check: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=3)


def test_command_entered_during_failure_redraw_is_not_lost(
    session: core.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Offline(Echo):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            raise ConnectionError("offline")

    session._provider = Offline()
    with terminal(monkeypatch) as (pipe, _):
        t = tui.Terminal(session)
        t.pending.append(tui.QueuedInput("A", {}))

        async def redraw(func: Callable[[], None]) -> None:
            await run_in_terminal(func)
            if t.paused and not t.running:
                pipe.send_text("/retry\r")
                await until(lambda: t.session.app.is_done)

        monkeypatch.setattr(tui, "run_in_terminal", redraw)
        assert t.turn("T") == "/retry"
        assert t.paused and [i.text for i in t.pending] == ["A"]


@pytest.mark.parametrize("paused", [False, True])
def test_capacity_recall_attachments_and_original_up(
    session: core.Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paused: bool
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("original snapshot")
    item = attachments.Attachment.read(source)
    with terminal(monkeypatch) as (pipe, _):
        t = tui.Terminal(session)
        t.running = not paused
        t.paused = paused

        async def keys() -> None:
            task = asyncio.create_task(t.session.prompt_async())
            try:
                await until(lambda: t.session.app.is_running)
                pipe.send_text("A\rB\r")
                await until(lambda: len(t.pending) == 2)
                t.add_attachments([item])
                pipe.send_text("C\r")
                await until(lambda: len(t.pending) == 3)
                source.unlink()
                t.add_attachments([item])
                pipe.send_text("draft\r")
                await until(lambda: "3/3" in t.notice)
                assert t.session.default_buffer.text == "[file1]draft"
                assert t.attachments == {"[file1]": item}
                assert len(session.log.state.nodes) == 1, "queuing must not create nodes"

                pipe.send_text("\x1b[A")
                await until(lambda: len(t.pending) == 2)
                assert t.session.default_buffer.text == "[file1]C"
                assert t.attachments == {"[file1]": item}
                t.add_attachments([item])
                assert "[file2]" in t.session.default_buffer.text
                assert item.raw() == b"original snapshot"
                pipe.send_text("\x1b[A")
                await until(lambda: len(t.pending) == 1)
                assert t.session.default_buffer.text == "B" and not t.attachments
                pipe.send_text("\x1b[A")
                await until(lambda: not t.pending)
                assert t.session.default_buffer.text == "A"
                pipe.send_text("\x1b[A")
                await until(lambda: t.session.default_buffer.text != "A")
                assert t.session.default_buffer.text == "[file1]C", "Up returns to input history"
            finally:
                t.session.app.exit(result="")
                await task

        asyncio.run(keys())


@pytest.mark.parametrize("language", ["zh", "en"])
def test_queue_previews_are_single_lines_and_commands_cannot_queue(
    session: core.Session, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    from kaipi import settings

    settings.save({"language": language})
    with terminal(monkeypatch) as (pipe, _):
        t = tui.Terminal(session)
        t.running = True

        async def keys() -> None:
            task = asyncio.create_task(t.session.prompt_async())
            try:
                await until(lambda: t.session.app.is_running)
                pipe.send_text("/new\r")
                await until(lambda: bool(t.notice))
                assert not t.pending and t.session.default_buffer.text == "/new"
                pipe.send_text("\x15/explore question\r")
                await until(lambda: bool(t.pending))
                assert t.pending[0].text == "/explore question"
                t.pending.append(tui.QueuedInput("长行" * 200 + "\nsecond line", {}))
                rows = fragment_list_to_text(t.queue_rows()).splitlines()
                assert len(rows) == 2 and rows[-1].endswith("…")
                assert "second line" not in rows[-1]
                # Queue recall takes precedence even when slash completion is open.
                pipe.send_text("/go \x1b[A")
                await until(lambda: len(t.pending) == 1)
                assert t.session.default_buffer.text.endswith("\nsecond line")
            finally:
                t.session.app.exit(result="")
                await task

        asyncio.run(keys())


def test_turn_completion_waits_for_attachment_draft(
    session: core.Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "draft.txt"
    source.write_text("attachment snapshot")
    item = attachments.Attachment.read(source)
    model_started, model_release = threading.Event(), threading.Event()
    paste_started, paste_release = threading.Event(), threading.Event()

    class Provider(Echo):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            model_started.set()
            if not model_release.wait(3):
                raise RuntimeError("test input timed out")
            return super().complete(system, messages, points)

    def clipboard() -> tuple[list[attachments.Attachment], str]:
        paste_started.set()
        if not paste_release.wait(3):
            raise RuntimeError("test clipboard timed out")
        return [item], ""

    monkeypatch.setattr(attachments, "clipboard", clipboard)
    session._provider = Provider()
    stage = 0
    saw_finished_paste = False
    with terminal(monkeypatch) as (pipe, _):
        t = tui.Terminal(session)

        def rendered(app: Application[str]) -> None:
            nonlocal stage, saw_finished_paste
            if stage == 0 and model_started.is_set():
                pipe.send_text("A\r\x16")
                stage = 1
            elif stage == 1 and paste_started.is_set():
                model_release.set()
                stage = 2
            elif stage == 2 and t.done.is_set():
                saw_finished_paste = t._pasting
                paste_release.set()
                stage = 3

        t.session.app.after_render += rendered
        assert t.turn("T") == ""
        assert saw_finished_paste and t.phase == "已完成"
        assert t._draft == "[file1]" and t.attachments == {"[file1]": item}
        assert [i.text for i in t.pending] == ["A"]


def test_fifo_across_turns_keeps_unsent_draft_and_attachment(
    session: core.Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "draft.txt"
    source.write_text("draft attachment")
    item = attachments.Attachment.read(source)
    release = threading.Event()
    requests: list[list[Message]] = []
    questions: list[str] = []

    class Provider(Echo):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            questions.append(messages[-1]["content"][0]["text"])
            requests.append(messages)
            if questions[-1] in ("T", "A"):
                release.clear()
                if not release.wait(3):
                    raise RuntimeError("test input timed out")
            return super().complete(system, messages, points)

    session._provider = Provider()
    created: list[tui.Terminal] = []
    drafts: list[str] = []
    phase = 0
    with terminal(monkeypatch) as (pipe, _):
        original = tui.Terminal

        def build(s: core.Session) -> tui.Terminal:
            t = original(s)
            created.append(t)

            def rendered(app: Application[str]) -> None:
                nonlocal phase
                if phase == 0:
                    pipe.send_text("T\r")
                    phase = 1
                elif phase == 1 and questions == ["T"]:
                    pipe.send_text("A\rB\rC\r")
                    phase = 2
                elif phase == 2 and len(t.pending) == 3:
                    release.set()
                    phase = 3
                elif phase == 3 and questions[-1] == "A":
                    pipe.send_text("D\r")
                    phase = 4
                elif phase == 4 and [i.text for i in t.pending] == ["B", "C", "D"]:
                    t.add_attachments([item])
                    t.session.default_buffer.insert_text("unsent draft")
                    source.unlink()
                    release.set()
                    phase = 5
                elif phase == 5 and t.running and questions[-1] in ("B", "C", "D"):
                    drafts.append(app.current_buffer.text)
                elif phase == 5 and not t.running and questions[-1] == "D":
                    drafts.append(app.current_buffer.text)
                    t.attachments.clear()
                    pipe.send_text("\x15/quit\r")
                    phase = 6

            t.session.app.after_render += rendered
            return t

        monkeypatch.setattr(tui, "Terminal", build)
        assert tui.loop(session) == "quit"
    assert phase == 6 and questions == ["T", "A", "B", "C", "D"]
    assert drafts and set(drafts) == {"[file1]unsent draft"}
    nodes = list(session.log.state.nodes.values())
    assert [context.user_input(n) for n in nodes] == ["root", *questions]
    assert all(n.parent_id == p.id for p, n in zip(nodes, nodes[1:], strict=False))
    for index, request in enumerate(requests):
        inputs = [m["content"][0]["text"] for m in request if m["role"] == "user"]
        assert inputs == ["root", *questions[: index + 1]]


@pytest.mark.parametrize("explore", [False, True])
@pytest.mark.parametrize("action", ["continue", "retry"])
@pytest.mark.parametrize("failure", ["network", "escape"])
def test_failure_pauses_then_resumes_without_failed_context(
    session: core.Session,
    monkeypatch: pytest.MonkeyPatch,
    explore: bool,
    action: str,
    failure: str,
) -> None:
    root = session.leaf
    requests: list[list[Message]] = []
    release = threading.Event()

    class Provider(Echo):
        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            requests.append(messages)
            if len(requests) == 1:
                if not release.wait(3):
                    raise RuntimeError("test input timed out")
                if failure == "network":
                    raise RuntimeError("offline")
                raise KeyboardInterrupt  # a cancellable provider/tool observes Esc
            return super().complete(system, messages, points)

    session._provider = Provider()
    phase = 0
    paused: list[list[str]] = []
    with terminal(monkeypatch) as (pipe, _):
        original = tui.Terminal

        def build(s: core.Session) -> tui.Terminal:
            t = original(s)

            def rendered(app: Application[str]) -> None:
                nonlocal phase
                if phase == 0:
                    pipe.send_text(("/explore T" if explore else "T") + "\r")
                    phase = 1
                elif phase == 1 and requests:
                    pipe.send_text("A\rB\rC\r")
                    phase = 2
                elif phase == 2 and len(t.pending) == 3:
                    if failure == "escape":
                        pipe.send_text("\x1b")
                    else:
                        release.set()
                    phase = 3
                elif phase == 3 and failure == "escape" and t.stop.is_set():
                    release.set()
                if phase == 3 and t.paused and not t.running:
                    paused.append([i.text for i in t.pending])
                    pipe.send_text(f"/{action}\r")
                    phase = 4
                elif (
                    phase == 4
                    and not t.running
                    and len(requests) == (5 if action == "retry" else 4)
                ):
                    pipe.send_text("/quit\r")
                    phase = 5

            t.session.app.after_render += rendered
            return t

        monkeypatch.setattr(tui, "Terminal", build)
        assert tui.loop(session) == "quit"
    assert phase == 5 and paused and all(items == ["A", "B", "C"] for items in paused)
    nodes = list(session.log.state.nodes.values())
    dead = nodes[1]
    assert dead.status == "aborted" and dead.parent_id == root
    first = nodes[2]
    assert first.parent_id == root, "continue/retry creates a sibling of the failed turn"
    assert first.request and first.request.exploration == explore
    assert graph.trunk(session.log.state) == (root if explore else session.leaf)
    assert [context.user_input(n) for n in nodes[2:]] == (["T"] if action == "retry" else []) + [
        "A",
        "B",
        "C",
    ]
    # Only an explicit retry repeats T; continuing must not send its failed input.
    resumed_inputs = [
        b["text"]
        for m in requests[1]
        if m["role"] == "user"
        for b in m["content"]
        if b["type"] == "text" and context.classify(b["text"]) == "user"
    ]
    assert resumed_inputs == ["root", "T" if action == "retry" else "A"]
    restored = core.Session(session.cwd, resume=session.log.path.stem)
    assert restored.leaf == session.leaf and len(restored.log.state.nodes) == len(nodes)
