from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.base import PipeInput
from prompt_toolkit.output.vt100 import Vt100_Output

from kaipi import catalog, cli, command_ui, ledger, providers, settings, tui
from tests.conftest import Builder

ENTER, LEAVE = "\x1b[?1049h", "\x1b[?1049l"


@contextmanager
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[PipeInput, io.StringIO]]:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("KAIPI_PLAIN", raising=False)
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=90), enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        print("previous conversation")
        yield pipe, stream


@pytest.fixture
def session(tmp_path: Path) -> cli.Session:
    s = cli.Session(tmp_path)
    s.leaf = Builder(s.log).node(None, "original question")
    s.pricing.models["fake"] = ledger.Price(input=1, output=2)
    s.save()
    providers.save_auth("deepseek", "https://example.test", "test-key", ["deepseek-flash"])
    providers.use_model("deepseek", "deepseek-flash")
    return s


def primary_screen(stream: io.StringIO) -> str:
    before, panel = stream.getvalue().split(ENTER)
    _, after = panel.split(LEAVE)
    # No global scrollback clear; returning must reveal the original conversation.
    assert "\x1b[3J" not in stream.getvalue()
    return before + after


@pytest.mark.parametrize("command", sorted(command_ui.MENUS))
def test_cancel_menu_leaves_no_conversation_output(
    session: cli.Session, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        before = session.log.path.read_bytes(), providers.auth_file().read_bytes(), session.leaf
        pipe.send_text("\x1b")
        assert command_ui.run(session, [command])
        assert "previous conversation" in primary_screen(stream)
        assert "已取消" not in primary_screen(stream)
        assert "Esc" not in primary_screen(stream)
        assert command not in primary_screen(stream)
        assert before == (
            session.log.path.read_bytes(),
            providers.auth_file().read_bytes(),
            session.leaf,
        )


@pytest.mark.parametrize("command", [*sorted(command_ui.QUERIES), "trunk"])
def test_query_scrolls_and_closes_without_residue(
    session: cli.Session,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        monkeypatch.setattr(cli, "verb", lambda *_: [f"query row {n}" for n in range(100)])
        pipe.send_text("\x1b[6~\x1b[6~\x1b")
        assert command_ui.run(session, [command])
        assert "query row" in stream.getvalue()
        assert "query row" not in primary_screen(stream)
        assert "previous conversation" in primary_screen(stream)


def test_model_success_keeps_only_result(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        pipe.send_text("\r")
        assert command_ui.run(session, ["model"])
        restored = primary_screen(stream)
        assert "已启用" in restored and "deepseek-flash" in restored
        assert "选择模型" not in restored and "Esc" not in restored


def test_provider_refresh_keeps_only_result(
    session: cli.Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        monkeypatch.setattr(
            catalog,
            "fetch",
            lambda _: {"new-model": ledger.Price(provider="deepseek", input=1, output=2)},
        )
        index = list(providers.BUILTIN).index("deepseek") + 1
        pipe.send_text(f"{index}\r\r\r")
        assert command_ui.run(session, ["provider"])
        assert "new-model" in providers.auth()["providers"]["deepseek"]["models"]
        restored = primary_screen(stream)
        assert "已刷新" in restored
        assert "正在读取" not in restored and "new-model" not in restored
        assert "test-key" not in stream.getvalue()


def test_rewind_cancel_after_preview_restores_screen_and_state(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        before = session.log.path.read_bytes(), session.leaf
        pipe.send_text("\x1b")
        assert command_ui.run(session, ["rewind", "N1"])
        assert "rewind" not in primary_screen(stream)
        assert before == (session.log.path.read_bytes(), session.leaf)


def test_settings_save_without_leaking_menu(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        pipe.send_text("\x1b[D\x1b")
        assert command_ui.run(session, ["config"])
        assert not settings.auto_check_updates()
        assert "kaipi 设置" not in primary_screen(stream)


def test_command_failure_restores_screen(
    session: cli.Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with terminal(monkeypatch) as (pipe, stream):

        def fail(*args: object, **kwargs: object) -> bool:
            print("temporary progress")
            raise ValueError("operation failed")

        monkeypatch.setattr(cli, "slash", fail)
        with pytest.raises(ValueError, match="operation failed"):
            command_ui.run(session, ["provider"])
        assert "temporary progress" not in primary_screen(stream)


def test_export_path_remains_on_primary_screen(
    session: cli.Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        monkeypatch.setattr(cli, "verb", lambda *_: ["exported /workspace/context.md"])
        assert command_ui.run(session, ["inspect-context"])
        assert ENTER not in stream.getvalue()
        assert "/workspace/context.md" in stream.getvalue()


def test_slash_commands_do_not_echo_but_questions_do(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    with terminal(monkeypatch) as (pipe, stream):
        t = tui.Terminal(session)
        t.echo("/config")
        t.echo("ordinary question")
        assert "/config" not in stream.getvalue()
        assert "ordinary question" in stream.getvalue()
