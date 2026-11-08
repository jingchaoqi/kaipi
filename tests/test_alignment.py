from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import Application, create_app_session
from prompt_toolkit.formatted_text import ANSI, fragment_list_to_text, to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from kaipi import catalog, configuration, core, ledger, providers, settings, views
from kaipi.display import columns
from kaipi.i18n import tr


def plain(text: str) -> str:
    return fragment_list_to_text(to_formatted_text(ANSI(text)))


def cell_at(row: str, text: str) -> int:
    row = plain(row)
    return get_cwidth(row[: row.index(text)])


def test_columns_handle_wide_combining_and_colored_text() -> None:
    rows = columns([("中文", "A"), ("e\u0301", "B"), ("\033[32mabc\033[0m", "C")])
    assert [cell_at(row, text) for row, text in zip(rows, "ABC", strict=True)] == [6, 6, 6]
    assert "\033[32m" in rows[2]
    assert columns([]) == []


@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("command", ["config", "statusline"])
def test_settings_values_align_in_rendered_menu(
    lang: str, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.save({"language": lang})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    original = Application.run
    frames: list[str] = []

    def run(app: Application[Any], **kwargs: Any) -> Any:
        def rendered(app: Application[Any]) -> None:
            if app.is_done:
                return
            control = app.layout.current_control
            frames.append(fragment_list_to_text(to_formatted_text(control.text)))  # type: ignore[attr-defined]
            app.exit()

        app.after_render += rendered
        return original(app, **kwargs)

    monkeypatch.setattr(Application, "run", run)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        getattr(configuration, command)()
    assert frames
    rows = frames[0].splitlines()
    if command == "config":
        labels = [tr("每日自动检查更新"), tr("语言")]
    else:
        labels = [tr(title) for title in settings.STATUSLINE_ITEMS.values()]
    starts = []
    for label in labels:
        row = next(row for row in rows if label in row)
        end = row.index(label) + len(label)
        starts.append(get_cwidth(row[:end]) + len(row[end:]) - len(row[end:].lstrip()))
    assert len(set(starts)) == 1


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_model_columns_support_long_names_and_mixed_width(
    lang: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.save({"language": lang})
    pairs = [
        ("openai", "short"),
        ("deepseek-anthropic", "very-long-model-name-超过旧列宽-vision-exp"),
    ]
    monkeypatch.setattr(providers, "configured_models", lambda: pairs)
    monkeypatch.setattr(providers, "active_model", lambda: "openai/short")
    monkeypatch.setattr(providers, "use_model", lambda *_: None)
    pricing = ledger.Pricing(
        models={f"{who}/{name}": ledger.Price(input=0.5, output=2) for who, name in pairs}
    )
    monkeypatch.setattr(ledger.Pricing, "load", lambda: pricing)
    rows: list[str] = []

    def choose(values: list[str], current: int) -> int:
        rows.extend(values)
        return current

    monkeypatch.setattr(configuration, "choose", choose)
    configuration.model(report=lambda _: None)
    assert cell_at(rows[0], "openai") == cell_at(rows[1], "deepseek-anthropic")
    cost = tr("in {v0}/out {v1} 每百万", v0=0.5, v1=2.0)
    assert cell_at(rows[0], cost) == cell_at(rows[1], cost)


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_provider_menu_and_saved_list_align(
    lang: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.save({"language": lang})
    providers.save_auth("openai", "https://short.example", "test", ["first-model"])
    providers.save_auth(
        "deepseek-anthropic", "https://long.example/anthropic", "test", ["second-model"]
    )
    rows: list[str] = []

    def choose(values: list[str]) -> int:
        rows.extend(values)
        raise KeyboardInterrupt

    monkeypatch.setattr(configuration, "choose_provider", choose)
    with pytest.raises(KeyboardInterrupt):
        configuration.provider()
    starts = []
    urls = []
    for row, (name, cfg) in zip(rows, providers.BUILTIN.items(), strict=True):
        starts.append(get_cwidth(plain(row)[: plain(row).index(cfg.api, len(name))]))
        urls.append(cell_at(row, cfg.base_url or tr("(官方默认)")))
    assert len(set(starts)) == len(set(urls)) == 1
    saved = [row for row in capsys.readouterr().out.splitlines() if " · " in row]
    assert cell_at(saved[0], "https://") == cell_at(saved[1], "https://")
    assert cell_at(saved[0], "first-model") == cell_at(saved[1], "second-model")


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_refresh_price_list_aligns(
    lang: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.save({"language": lang})
    monkeypatch.setattr(
        catalog,
        "fetch",
        lambda _: {name: ledger.Price(input=0.5, output=2) for name in ["small", "很长的模型名称"]},
    )

    def cancel(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(configuration, "prompt", cancel)
    with pytest.raises(KeyboardInterrupt):
        configuration.refresh("openai", [])
    cost = tr("in {v0}/out {v1} 每百万", v0=0.5, v1=2.0)
    rows = [row for row in capsys.readouterr().out.splitlines() if cost in row]
    assert len(rows) == 2 and cell_at(rows[0], cost) == cell_at(rows[1], cost)


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_help_descriptions_still_align(lang: str, tmp_path: Path) -> None:
    settings.save({"language": lang})
    rows = views.cmd_help(core.Session(tmp_path))
    starts = [
        cell_at(row, tr(command[2]))
        for row, command in zip(rows[: len(views.COMMANDS)], views.COMMANDS, strict=True)
    ]
    assert len(set(starts)) == 1
