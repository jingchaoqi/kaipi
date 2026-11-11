from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kaipi import cli, core, settings, tui
from kaipi.model import SessionTitleGenerated
from tests.conftest import Builder
from tests.test_command_ui import terminal


@pytest.mark.parametrize("language", ["zh", "en"])
def test_tui_title_tracks_session_changes_without_repeated_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    settings.save({"language": language})
    s = core.Session(tmp_path, new=True)
    previous = s.log.path.stem
    with terminal(monkeypatch) as (_, stream):
        t = tui.Terminal(s)

        def render() -> str:
            t.session.app.before_render.fire()
            t.session.output.flush()
            return stream.getvalue()

        assert "\033]2;kaipi\007" in render()
        for _ in range(60):
            render()
        assert stream.getvalue().count("\033]2;") == 1
        s.leaf = Builder(s.log).node(None, "first question")
        s.log.append(SessionTitleGenerated(name="自动生成标题", model="fake"))
        assert "\033]2;kaipi丨自动生成标题\007" in render()
        s.rename("排查问题\n第二行\x1b\x07\x9c")
        assert "\033]2;kaipi丨排查问题 第二行\007" in render()
        assert "\x9c" not in stream.getvalue()
        s.new()
        assert render().endswith("\033]2;kaipi\007")
        s.resume(previous)
        assert render().endswith("\033]2;kaipi丨排查问题 第二行\007")


@pytest.mark.parametrize("tty,term", [(True, "xterm-256color"), (False, "xterm"), (True, "dumb")])
def test_plain_loop_updates_title_only_on_supported_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tty: bool,
    term: str,
) -> None:
    s = core.Session(tmp_path, new=True)
    s.leaf = Builder(s.log).node(None, "old question")
    s.rename("旧会话")
    previous = s.log.path.stem
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    monkeypatch.setenv("TERM", term)
    monkeypatch.setenv("KAIPI_PLAIN", "1")
    lines = iter(["/rename 新名称", "/new", f"/resume {previous}", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(lines))
    assert cli.interactive(s) == "quit"
    output = capsys.readouterr().out
    if tty and term != "dumb":
        for name in ("旧会话", "新名称"):
            assert f"\033]2;kaipi丨{name}\007" in output
        assert "\033]2;kaipi\007" in output
        assert output.count("\033]2;") == 4
    else:
        assert "\033]2;" not in output
