from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from kaipi import cli, context, core, history, ledger, settings, tui, update
from kaipi.attachments import TAG as ATTACHMENT_TAG
from kaipi.model import NodeCompleted, TrunkPinned, Usage
from kaipi.store import Log
from tests.conftest import Builder


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> core.Session:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAIPI_MODEL", "claude-opus-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(core.Session, "_build", lambda *_: pytest.fail("history called a model"))
    s = core.Session(tmp_path, new=True)
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


def test_replay_is_read_only_and_follows_the_saved_branch(session: core.Session) -> None:
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
        ("ledger", "[N1 主干] $0.0000  缓存读取 0.0k/0.1k  上下文 0.1k"),
        ("N1>", "second question [image1]"),
        ("cmd", "touch must-not-run"),
        ("text", "```python\nprint('restored answer')\n```"),
        ("ledger", "[N2 主干] $0.0000  缓存读取 0.0k/0.1k  上下文 0.1k"),
    ]
    assert not (session.cwd / "must-not-run").exists()
    assert session.log.state == state
    assert (session.log.path.read_bytes(), session.cursor_path.read_bytes()) == before
    assert ledger.report(session.log.state, session.pricing).usage == usage


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("branch", [False, True])
def test_replay_summary_uses_each_nodes_saved_usage(
    session: core.Session, language: str, branch: bool
) -> None:
    settings.save({"language": language})
    node = session.log.state.nodes[session.leaf or ""]
    session.log.append(
        NodeCompleted(
            id=node.id,
            payload=node.payload,
            usage=Usage(input_uncached=300, cache_read=1200, output=200),
            context_tokens=750,
        )
    )
    if branch:
        session.log.append(TrunkPinned(node_id=node.parent_id))
    session.pricing.models["fake"] = ledger.Price(input=2, cache_read=0.5, output=6)
    before = session.log.path.read_bytes()
    events: list[tuple[str, str]] = []
    history.replay(session, lambda *_: None, lambda kind, text: events.append((kind, text)))
    summaries = [text for kind, text in events if kind == "ledger"]
    kind = (
        ("探索" if branch else "主干")
        if language == "zh"
        else ("exploration" if branch else "trunk")
    )
    details = (
        "缓存读取 1.2k/1.5k  上下文 0.8k"
        if language == "zh"
        else "cache read 1.2k/1.5k  context 0.8k"
    )
    assert summaries[-1] == f"[N2 {kind}] $0.0024  {details}"
    assert "$0.0003" in summaries[0] and summaries[0].endswith("0.1k")
    assert session.log.path.read_bytes() == before


@pytest.mark.parametrize("args", [["-c"], ["-r"]])
def test_resume_cli_prints_history_before_the_prompt(
    session: core.Session, args: list[str]
) -> None:
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
    session: core.Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    session: core.Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interactive: bool,
) -> None:
    old = session.log.path
    fresh = core.Session(session.cwd, new=True)
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


def test_attachment_data_and_thinking_are_not_replayed(session: core.Session) -> None:
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
    s = core.Session(tmp_path, new=True)
    s.log.path.write_bytes(fixture.read_bytes())
    s.log = Log(s.log.path)
    s._place()
    events: list[str] = []
    monkeypatch.setattr(core.Session, "_build", lambda *_: pytest.fail("history called a model"))
    history.replay(s, lambda _, text: events.append(text), lambda _, text: events.append(text))
    assert events and events[-1].startswith("[N")
    assert s.log.path.read_bytes() == fixture.read_bytes()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="requires a terminal")
@pytest.mark.parametrize("option", ["-r", "-c", "update"])
def test_real_terminal_resume_shows_transcript_before_input(
    session: core.Session, option: str
) -> None:
    import fcntl
    import select
    import struct
    import termios
    import time

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    args = [option, session.log.path.stem] if option == "-r" else [option]
    command = [sys.executable, "-c", "from kaipi.cli import app; app()", *args]
    if option == "update":
        installer, executable = session.cwd / "fake-uv", session.cwd / "kaipi"
        installer.write_text(f"#!{sys.executable}\n")
        executable.write_text(f"#!{sys.executable}\nfrom kaipi.cli import app\napp()\n")
        installer.chmod(0o755)
        executable.chmod(0o755)
        command = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            update.WORKER,
            str(installer),
            str(session.cwd / "wheel.whl"),
            str(executable),
            "v0.1.5",
            session.log.path.stem,
            str(session.cwd / "tools"),
            json.dumps([""] * 5),
        ]
    before = session.log.path.read_bytes()
    child = subprocess.Popen(
        command,
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
        assert "[N1 主干] $0.0000  缓存读取 0.0k/0.1k" in rendered
        assert "[N2 主干] $0.0000  缓存读取 0.0k/0.1k" in rendered
        assert "[N1]" not in rendered and "[N2]" not in rendered
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
