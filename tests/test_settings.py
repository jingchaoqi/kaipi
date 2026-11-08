from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session, get_app
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from kaipi import cli, configuration, core, settings, tui, update, views


def test_preference_persists_and_explicit_choice_overrides_legacy_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KAIPI_NO_UPDATE_CHECK")
    assert settings.auto_check_updates()
    monkeypatch.setenv("KAIPI_NO_UPDATE_CHECK", "1")
    assert not settings.auto_check_updates()
    settings.set_auto_check_updates(True)
    assert settings.auto_check_updates()
    assert json.loads(settings.path().read_text())["auto_check_updates"] is True
    settings.set_auto_check_updates(False)
    monkeypatch.delenv("KAIPI_NO_UPDATE_CHECK")
    assert not settings.auto_check_updates()


@pytest.mark.parametrize(
    "keys,changes",
    [
        ("\x1b", []),
        ("\x1b[A\x1b[B\x1b", []),
        ("\x1b[D\x1b", [False]),
        ("\x1b[D\x1b[C\x1b", [False, True]),
        ("\x1b[C\x1b[C\x1b", []),
    ],
)
def test_panel_saves_on_arrows_before_escape_without_enter(
    monkeypatch: pytest.MonkeyPatch, keys: str, changes: list[bool]
) -> None:
    monkeypatch.delenv("KAIPI_NO_UPDATE_CHECK")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    saved: list[bool] = []
    checks: list[bool] = []
    write = settings.set_auto_check_updates

    def immediate(enabled: bool) -> None:
        assert get_app().is_running and not get_app().is_done
        write(enabled)
        assert settings.auto_check_updates() is enabled
        saved.append(enabled)

    monkeypatch.setattr(settings, "set_auto_check_updates", immediate)
    monkeypatch.setattr(
        update, "background", lambda emit: checks.append(settings.auto_check_updates())
    )
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        cli.config()
    assert saved == changes
    assert settings.auto_check_updates() is (changes[-1] if changes else True)
    assert checks == [value for value in changes if value]
    assert settings.path().exists() == bool(changes)


def test_panel_save_failure_keeps_effective_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.set_auto_check_updates(True)
    before = settings.path().read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def fail(enabled: bool) -> None:
        raise OSError("read-only directory")

    monkeypatch.setattr(settings, "set_auto_check_updates", fail)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[D\x1b")
        configuration.config()
    assert settings.auto_check_updates()
    assert settings.path().read_bytes() == before


def test_config_command_slash_help_and_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(configuration, "config", lambda: calls.append(True))
    result = CliRunner().invoke(cli.app, ["config"])
    assert result.exit_code == 0 and calls == [True]
    assert not (tmp_path / ".kaipi").exists()
    session = core.Session(tmp_path)
    assert cli.slash(session, ["config"])
    assert calls == [True, True]
    completions = tui.SlashCompleter(session).get_completions(Document("/conf"), CompleteEvent())
    assert [item.text for item in completions] == ["/config"]
    assert any("/config" in line for line in views.cmd_help(session))


def test_disable_suppresses_in_flight_notice_and_enable_works_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    workers: list[threading.Thread] = []
    thread = threading.Thread
    notices: list[str] = []
    calls: list[bool] = []

    def capture(**kwargs: Any) -> threading.Thread:
        worker = thread(**kwargs)
        workers.append(worker)
        return worker

    def check(*, force: bool = False) -> update.Release:
        calls.append(force)
        started.set()
        assert release.wait(3)
        return update.Release("v999.0.0", {})

    monkeypatch.setattr(update, "check", check)
    monkeypatch.setattr(threading, "Thread", capture)
    settings.set_auto_check_updates(True)
    update.background(notices.append)
    try:
        assert started.wait(2)
        update.background(notices.append)  # no concurrent network request
        workers[-1].join(2)
        assert calls == [False]
        settings.set_auto_check_updates(False)
        release.set()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
        assert not notices and "notified" not in update._read_cache()
        count = len(workers)
        update.background(notices.append)
        assert len(workers) == count
        settings.set_auto_check_updates(True)
        update.background(notices.append)
        workers[-1].join(2)
        assert len(notices) == 1 and calls == [False, False]
        settings.set_auto_check_updates(False)
        manual = update.check(force=True)
        assert manual is not None and manual.tag == "v999.0.0"  # manual checks remain available
    finally:
        release.set()
        for worker in workers:
            worker.join(2)


@pytest.mark.parametrize(
    "keys,provider,model",
    [
        ("\x1b[D\x1b[B\x1b[D\x1b", False, False),
        ("\x1b[D\x1b[C\x1b[B\x1b[D\x1b[A\x1b[C\x1b", False, False),
    ],
)
def test_statusline_draft_is_saved_once_on_escape(
    monkeypatch: pytest.MonkeyPatch,
    keys: str,
    provider: bool,
    model: bool,
) -> None:
    settings.save({"auto_check_updates": False, "future_preference": "keep"})
    before = settings.path().read_bytes()
    real_save = settings.save
    writes: list[dict[str, Any]] = []

    def save(changes: dict[str, Any]) -> None:
        assert get_app().is_running and not get_app().is_done
        assert settings.path().read_bytes() == before  # no writes while moving/toggling
        writes.append(changes)
        real_save(changes)

    monkeypatch.setattr(settings, "save", save)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        cli.statusline()
    assert len(writes) == 1
    assert settings.statusline() == {
        key: ("on" if provider else "off")
        if key == "provider"
        else ("on" if model else "off")
        if key == "model"
        else next(iter(settings.STATUSLINE_OPTIONS[key]))
        for key in settings.STATUSLINE_ITEMS
    }
    assert settings.read()["future_preference"] == "keep"
    assert not settings.auto_check_updates()


@pytest.mark.parametrize("cancel", ["\x03", "\x04"])
def test_statusline_cancel_discards_draft(monkeypatch: pytest.MonkeyPatch, cancel: str) -> None:
    settings.save({"auto_check_updates": True})
    before = settings.path().read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[D" + cancel)
        configuration.statusline()
    assert settings.path().read_bytes() == before


def test_statusline_failed_save_can_be_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    real_save = settings.save
    attempts = 0

    def save(changes: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk full")
        real_save(changes)

    monkeypatch.setattr(settings, "save", save)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[D\x1b\x1b")
        configuration.statusline()
    assert attempts == 2 and settings.statusline()["provider"] == "off"


@pytest.mark.parametrize("direction", ["\x1b[C", "\x1b[D"])
@pytest.mark.parametrize("field", list(settings.STATUSLINE_ITEMS))
def test_statusline_arrows_cycle_and_wrap(
    monkeypatch: pytest.MonkeyPatch, field: str, direction: str
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    modes = list(settings.STATUSLINE_OPTIONS[field])
    if direction == "\x1b[D":
        modes = [modes[0], *reversed(modes[1:])]
    down = "\x1b[B" * list(settings.STATUSLINE_ITEMS).index(field)
    for expected in [*modes[1:], modes[0]]:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(down + direction + "\x1b")
            configuration.statusline()
        assert settings.statusline()[field] == expected


def test_statusline_reads_legacy_and_invalid_modes() -> None:
    settings.save(
        {"statusline": {"provider": True, "model": False, "directory": True, "context": False}}
    )
    assert settings.statusline() == {
        key: "off" if key in ("model", "context") else "full" if key == "directory" else "on"
        for key in settings.STATUSLINE_ITEMS
    }
    settings.save({"statusline": {"directory": [], "context": "compact", "provider": 1}})
    assert settings.statusline()["directory"] == "full"
    assert settings.statusline()["context"] == "full"
    assert settings.statusline()["provider"] == "on"


@pytest.mark.parametrize(
    "field,mode,used,limit,expected",
    [
        ("directory", "compact", 0, 0, "project"),
        ("directory", "off", 0, 0, ""),
        ("context", "full", 12500, 100000, "上下文 12.5k/100k"),
        ("context", "percent", 12500, 100000, "上下文 12.5%"),
        ("context", "percent", 0, 100000, "上下文 0.0%"),
        ("context", "percent", 125000, 100000, "上下文 125.0%"),
        ("context", "percent", 12500, 0, "上下文 未知"),
        ("context", "off", 12500, 100000, ""),
    ],
)
def test_statusline_directory_and_context_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mode: str,
    used: int,
    limit: int,
    expected: str,
) -> None:
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    from kaipi import ledger

    session = core.Session(tmp_path)
    session.cwd = tmp_path / "project"
    monkeypatch.setattr(ledger, "trunk_burden", lambda state: used)
    monkeypatch.setattr(ledger.Pricing, "price", lambda self, spec: ledger.Price(context=limit))
    settings.save({"statusline": dict.fromkeys(settings.STATUSLINE_ITEMS, "off") | {field: mode}})
    assert fragment_list_to_text(to_formatted_text(ANSI(views.status(session, 300)))) == expected


@pytest.mark.parametrize("field", list(settings.STATUSLINE_ITEMS))
def test_statusline_elements_are_independently_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    from kaipi.model import SessionRenamed

    monkeypatch.setenv("KAIPI_MODEL", "openai/example-model")
    session = core.Session(tmp_path)
    session.log.append(SessionRenamed(name="对话名字"))
    settings.save({"statusline": {key: key == field for key in settings.STATUSLINE_ITEMS}})
    shown = fragment_list_to_text(to_formatted_text(ANSI(views.status(session, 300))))
    expected = {
        "provider": "openai",
        "model": "example-model",
        "session": "对话名字",
        "directory": str(tmp_path),
        "context": "上下文 0.0k",
        "spent": "累计 $0.0000",
        "saved_tokens": "预计节省 0.0k/轮",
        "saved_cost": "预计节省 $0.0000",
    }
    assert shown == expected[field]


def test_all_hidden_keeps_activity_row_and_does_not_change_status_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = core.Session(tmp_path)
    report = views.cmd_status(session)
    settings.save({"statusline": dict.fromkeys(settings.STATUSLINE_ITEMS, False)})
    assert views.status(session) == "" and views.cmd_status(session) == report
    with create_app_session(output=DummyOutput()):
        terminal = tui.Terminal(session)
        calls = 0

        def status(s: core.Session, width: int = 120) -> str:
            nonlocal calls
            calls += 1
            return ""

        monkeypatch.setattr(views, "status", status)
        first = terminal.toolbar()
        second = terminal.toolbar()
        assert "就绪" in "".join(fragment[1] for fragment in first.__pt_formatted_text__())
        assert "等待输入" in "".join(fragment[1] for fragment in second.__pt_formatted_text__())
        assert calls == 1


def test_statusline_dispatch_completion_and_immediate_bar_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(configuration, "statusline", lambda: calls.append(True))
    assert CliRunner().invoke(cli.app, ["statusline"]).exit_code == 0
    assert calls == [True] and not (tmp_path / ".kaipi").exists()
    session = core.Session(tmp_path)
    fake = SimpleNamespace(_bar_at=99.0)
    count = 0

    def prompt() -> str:
        nonlocal count
        count += 1
        if count == 1:
            return "/statusline"
        assert fake._bar_at == 0
        return "/quit"

    fake.prompt = prompt
    monkeypatch.setattr(tui, "Terminal", lambda s: fake)
    assert tui.loop(session) == "quit"
    assert calls == [True, True]
    completions = tui.SlashCompleter(session).get_completions(Document("/statusl"), CompleteEvent())
    assert [item.text for item in completions] == ["/statusline"]
