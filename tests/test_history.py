from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from kaipi import cli, context, history, ledger, tui
from kaipi.attachments import TAG as ATTACHMENT_TAG
from kaipi.model import NodeCompleted, TrunkPinned
from kaipi.store import Log
from tests.conftest import Builder


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cli.Session:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAIPI_MODEL", "claude-opus-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(cli.Session, "_build", lambda *_: pytest.fail("history called a model"))
    s = cli.Session(tmp_path, new=True)
    builder = Builder(s.log)
    root = builder.node(None, "first question", answer="**first answer**")
    leaf = builder.node(
        root,
        "second question [image1]",
        answer="```python\nprint('restored answer')\n```",
        tool=("t1", "touch must-not-run", "hidden tool output"),
        notice=context.GUARD_TAG + "private guard",
    )
    builder.node(root, "other branch question", answer="other branch answer")
    s.log.append(TrunkPinned(node_id=leaf))
    s.leaf = leaf
    s.save()
    return s


def test_replay_is_read_only_and_follows_the_saved_branch(session: cli.Session) -> None:
    before = session.log.path.read_bytes(), session.cursor_path.read_bytes()
    state = session.log.state.model_copy(deep=True)
    usage = ledger.report(state, session.pricing).usage
    events: list[tuple[str, str]] = []
    history.replay(
        session,
        lambda node, text: events.append((node + ">", text)),
        lambda kind, text: events.append((kind, text)),
    )
    assert events == [
        ("root>", "first question"),
        ("text", "**first answer**"),
        ("ledger", "[N1]"),
        ("N1>", "second question [image1]"),
        ("cmd", "touch must-not-run"),
        ("text", "```python\nprint('restored answer')\n```"),
        ("ledger", "[N2]"),
    ]
    assert not (session.cwd / "must-not-run").exists()
    assert session.log.state == state
    assert (session.log.path.read_bytes(), session.cursor_path.read_bytes()) == before
    assert ledger.report(session.log.state, session.pricing).usage == usage


@pytest.mark.parametrize("args", [["-c"], ["-r"]])
def test_resume_cli_prints_history_before_the_prompt(session: cli.Session, args: list[str]) -> None:
    before = session.log.path.read_bytes()
    if args == ["-r"]:
        args = [*args, session.log.path.stem]
    result = CliRunner().invoke(cli.app, args, input="/quit\n")
    assert result.exit_code == 0, result.output
    assert result.output.index("first question") < result.output.index("second question")
    assert result.output.index("restored answer") < result.output.index("N2>")
    assert "other branch" not in result.output and "private guard" not in result.output
    assert "hidden tool output" not in result.output
    assert session.log.path.read_bytes() == before


def test_terminal_history_reuses_message_and_markdown_rendering(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    questions: list[tuple[str, str | None]] = []
    answers: list[str] = []
    original_echo, original_markdown = tui.Terminal.echo, tui.markdown

    def echo(terminal: tui.Terminal, text: str, *, label: str | None = None) -> None:
        questions.append((text, label))
        original_echo(terminal, text, label=label)

    def markdown(text: str, width: int, **kwargs: Any) -> str:
        answers.append(text)
        return original_markdown(text, width, **kwargs)

    monkeypatch.setattr(tui.Terminal, "echo", echo)
    monkeypatch.setattr(tui, "markdown", markdown)
    monkeypatch.setattr(tui.Terminal, "prompt", lambda *_: "/quit")
    with create_app_session(output=DummyOutput()):
        assert tui.loop(session) == "quit"
    assert questions == [("first question", "root> "), ("second question [image1]", "N1> ")]
    assert answers == ["**first answer**", "```python\nprint('restored answer')\n```"]
    output = capsys.readouterr().out
    assert "touch must-not-run" in output and "other branch" not in output


@pytest.mark.parametrize("interactive", [False, True])
def test_resume_inside_session_renders_selected_history(
    session: cli.Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interactive: bool,
) -> None:
    old = session.log.path
    fresh = cli.Session(session.cwd, new=True)
    fresh.save()
    if interactive:
        inputs = iter([f"/resume {old.stem}", "/quit"])
        monkeypatch.setattr(tui.Terminal, "prompt", lambda *_: next(inputs))
        with create_app_session(output=DummyOutput()):
            assert tui.loop(fresh) == "quit"
        output = capsys.readouterr().out
    else:
        result = CliRunner().invoke(cli.app, [], input=f"/resume {old.stem}\n/quit\n")
        assert result.exit_code == 0, result.output
        output = result.output
    assert "first question" in output and "restored answer" in output
    assert "other branch question" not in output


def test_attachment_data_and_thinking_are_not_replayed(session: cli.Session) -> None:
    node = session.log.state.nodes[session.leaf or ""]
    payload = node.model_copy(deep=True).payload
    payload[0]["content"].extend(
        [
            {"type": "text", "text": ATTACHMENT_TAG + "private attachment body"},
            {"type": "image", "source": {"data": "private base64"}},
        ]
    )
    payload[-1]["content"].append({"type": "thinking", "thinking": "private thinking"})
    session.log.append(NodeCompleted(id=node.id, payload=payload, usage=node.usage))
    events: list[str] = []
    history.replay(
        session, lambda _, text: events.append(text), lambda _, text: events.append(text)
    )
    assert "second question [image1]" in events
    assert "private" not in "\n".join(events)


@pytest.mark.parametrize("version", ["v0.1.0", "v0.1.2"])
def test_legacy_session_payloads_can_be_replayed(
    tmp_path: Path, version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / f"{version}.jsonl"
    s = cli.Session(tmp_path, new=True)
    s.log.path.write_bytes(fixture.read_bytes())
    s.log = Log(s.log.path)
    s._place()
    events: list[str] = []
    monkeypatch.setattr(cli.Session, "_build", lambda *_: pytest.fail("history called a model"))
    history.replay(s, lambda _, text: events.append(text), lambda _, text: events.append(text))
    assert events and events[-1].startswith("[N")
    assert s.log.path.read_bytes() == fixture.read_bytes()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="requires a terminal")
@pytest.mark.parametrize("option", ["-r", "-c"])
def test_real_terminal_resume_shows_transcript_before_input(
    session: cli.Session, option: str
) -> None:
    import fcntl
    import select
    import struct
    import termios
    import time

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    args = [option, session.log.path.stem] if option == "-r" else [option]
    before = session.log.path.read_bytes()
    child = subprocess.Popen(
        [sys.executable, "-c", "from kaipi.cli import app; app()", *args],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 15
    try:
        while "累计".encode() not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                chunk = os.read(master, 65536)
                output += chunk
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[30;1R")
        rendered = output.decode(errors="replace")
        assert "累计" in rendered, rendered
        assert rendered.index("first question") < rendered.index("second question")
        assert rendered.index("restored answer") < rendered.index("N2>")
        assert "other branch question" not in rendered
        assert "private guard" not in rendered and "hidden tool output" not in rendered
        os.write(master, b"/quit\r")
        child.wait(timeout=5)
        assert child.returncode == 0
        assert session.log.path.read_bytes() == before
        assert not (session.cwd / "must-not-run").exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)
