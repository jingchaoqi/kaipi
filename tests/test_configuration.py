from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from kaipi import catalog, cli, configuration, core, ledger, providers

CATALOG = """
[models."deepseek-flash"]
provider = "deepseek"
input = 0.25
output = 1.0
[models."deepseek-new"]
provider = "deepseek"
input = 0.5
output = 2.0
[models."other-model"]
provider = "openai"
input = 1.0
output = 2.0
[providers.deepseek]
base_url = "https://untrusted.example"
api_key_env = "SECRET"
"""


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    providers.save_auth(
        "deepseek-anthropic",
        "https://chosen.example",
        "saved-secret",
        ["deepseek-flash", "custom-model"],
    )
    providers.use_model("deepseek-anthropic", "deepseek-flash")


@pytest.fixture
def online(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, *, timeout: int) -> httpx.Response:
        assert url == catalog.URL and timeout <= 10
        return httpx.Response(200, text=CATALOG, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)


def provider_keys(rest: str) -> str:
    return f"{list(providers.BUILTIN).index('deepseek-anthropic') + 1}\n{rest}"


@pytest.mark.parametrize(
    "command,keys",
    [
        ("provider", "\x1b\n"),
        ("provider", provider_keys("\x1b\n")),
        ("provider", provider_keys("2\n\x1b\n")),
        ("provider", provider_keys("2\nhttps://new.example\n\x1b\n")),
        ("provider", provider_keys("2\nhttps://new.example\nnew-key\n\x1b\n")),
        ("provider", provider_keys("\n\x1b\n")),
        ("model", "\x1b\n"),
    ],
)
def test_escape_leaves_configuration_untouched(
    configured: None, online: None, command: str, keys: str
) -> None:
    before = providers.auth_file().read_bytes()
    result = CliRunner().invoke(cli.app, [command], input=keys)
    assert result.exit_code == 0, result.output
    assert "Esc" in result.output and "已取消，配置未修改" in result.output
    assert providers.auth_file().read_bytes() == before
    assert not ledger.catalog_path().exists()


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\x1b[B\r", "custom-model"),
        ("\x1b[B\x1b[A\r", "deepseek-flash"),
        ("\x1b", "deepseek-flash"),
    ],
)
def test_model_arrow_selection_and_escape(
    configured: None, monkeypatch: pytest.MonkeyPatch, keys: str, expected: str
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        cli.cmd_model()
    assert providers.active_model() == f"deepseek-anthropic/{expected}"


def test_model_selection_starts_at_current(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers.use_model("deepseek-anthropic", "custom-model")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\r")
        cli.cmd_model()
    assert providers.active_model() == "deepseek-anthropic/custom-model"


def test_escape_key_without_enter_cancels_prompt(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = providers.auth_file().read_bytes()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b")
        assert not cli.cmd_provider()
    assert providers.auth_file().read_bytes() == before


def test_refresh_updates_catalog_and_model_list(configured: None, online: None) -> None:
    before = providers.auth()
    result = CliRunner().invoke(cli.app, ["provider"], input=provider_keys("\n\n"))
    assert result.exit_code == 0 and "已刷新" in result.output, result.output
    saved = providers.auth()["providers"]["deepseek-anthropic"]
    assert saved["models"] == ["deepseek-flash", "custom-model", "deepseek-new"]
    assert saved["api_key"] == "saved-secret" and saved["base_url"] == "https://chosen.example"
    assert providers.active_model() == before["model"]
    pricing = ledger.Pricing.load()
    assert pricing.price("deepseek-anthropic/deepseek-flash").input == 0.25
    assert pricing.price("deepseek-anthropic/deepseek-new").output == 2
    assert "other-model" not in ledger.catalog_prices()
    assert "deepseek" not in pricing.providers
    assert providers.auth_file().stat().st_mode & 0o777 == 0o600
    result = CliRunner().invoke(cli.app, ["model"], input="3\n")
    assert "deepseek-new" in result.output and "输入 0.5/输出 2.0" in result.output
    assert providers.active_model() == "deepseek-anthropic/deepseek-new"


@pytest.mark.parametrize(
    "body",
    ["not toml", "models = []", "", CATALOG.replace("0.25", "-1"), CATALOG.replace("0.25", "nan")],
)
def test_invalid_catalog_does_not_change_saved_data(
    configured: None, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    before = providers.auth_file().read_bytes()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(200, text=body, request=httpx.Request("GET", catalog.URL)),
    )
    result = CliRunner().invoke(cli.app, ["provider"], input=provider_keys("\n"))
    assert "刷新失败" in result.output
    assert providers.auth_file().read_bytes() == before
    assert not ledger.catalog_path().exists()


def test_offline_refresh_and_failed_save_preserve_data(
    configured: None, online: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices = catalog.fetch("deepseek-anthropic")
    catalog.save("deepseek-anthropic", ["deepseek-flash"], prices)
    before = ledger.catalog_path().read_bytes(), providers.auth_file().read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(providers, "save_auth", fail)
    with pytest.raises(OSError):
        catalog.save("deepseek-anthropic", ["new"], {"new": ledger.Price(input=3)})
    assert (ledger.catalog_path().read_bytes(), providers.auth_file().read_bytes()) == before
    monkeypatch.setattr(httpx, "get", fail)
    result = CliRunner().invoke(cli.app, ["provider"], input=provider_keys("\n"))
    assert "刷新失败" in result.output
    assert (ledger.catalog_path().read_bytes(), providers.auth_file().read_bytes()) == before


def test_refresh_preserves_price_overrides(configured: None, online: None) -> None:
    local = ledger.user_pricing()
    local.write_text('[models."deepseek-flash"]\nprovider="deepseek"\ninput=7\noutput=8\n')
    before = local.read_bytes()
    catalog.save(
        "deepseek-anthropic",
        ["deepseek-flash", "deepseek-new"],
        catalog.fetch("deepseek-anthropic"),
    )
    assert local.read_bytes() == before
    pricing = ledger.Pricing.load()
    assert pricing.price("deepseek-anthropic/deepseek-flash").input == 7
    assert pricing.price("deepseek-anthropic/deepseek-new").input == 0.5


def test_slash_refresh_reloads_current_session(
    configured: None, online: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = core.Session(tmp_path)
    before = session.pricing.price("deepseek-anthropic/deepseek-flash").input
    answers = iter([5, 1, ""])
    monkeypatch.setattr(configuration, "prompt", lambda *a, **k: next(answers))
    assert cli.slash(session, ["provider"])
    assert before != session.pricing.price("deepseek-anthropic/deepseek-flash").input
    assert session.pricing.price("deepseek-anthropic/deepseek-new").input == 0.5


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("5\r", 4),
        ("\x1b[B\r", 0),
        ("\x1b[A\r", 20),
        ("5\x1b[B\r", 5),
        ("5\x1b[A\r", 3),
        ("1\x1b[A\r", 0),
        ("21\x1b[B\r", 20),
        ("\r\x1b[B\r", 0),
        ("99\r\x15" + "2\r", 1),
    ],
)
def test_provider_accepts_numbers_and_arrows(
    monkeypatch: pytest.MonkeyPatch, keys: str, expected: int
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        assert configuration.choose_provider(list(providers.BUILTIN)) == expected


def test_provider_number_and_highlight_stay_in_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Any

    from prompt_toolkit import PromptSession

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    seen: list[tuple[str, str]] = []
    import threading
    import time

    steps = ["\x1b[B", "\x1b[B", "\x7f", "5", "\r"]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):

        def session(*args: Any, **kwargs: Any) -> PromptSession[str]:
            result: PromptSession[str] = PromptSession(*args, **kwargs)

            def frame(app: Any) -> None:
                if app.is_done:
                    return
                screen = app.renderer.last_rendered_screen
                display = "\n".join(
                    "".join(
                        screen.data_buffer[y][x].char for x in range(app.output.get_size().columns)
                    )
                    for y in range(screen.height)
                )
                if not seen or seen[-1][0] != result.default_buffer.text:
                    seen.append((result.default_buffer.text, display))

            result.app.after_render += frame
            return result

        def send() -> None:
            time.sleep(0.1)
            for key in steps:
                pipe.send_text(key)
                time.sleep(0.06)

        monkeypatch.setattr(configuration, "PromptSession", session)
        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        try:
            assert configuration.choose_provider(list(providers.BUILTIN)) == 4
        finally:
            worker.join(2)
    assert seen[0][0] == "" and "▸" not in seen[0][1]
    assert [text for text, _ in seen] == ["", "1", "2", "", "5"]
    for text, display in seen:
        if text:
            assert f"▸ {int(text):>2}." in display
        else:
            assert "▸" not in display


def test_existing_provider_action_menu_uses_arrows(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(configuration, "choose_provider", lambda rows: 4)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B\x1b[B\r")
        assert cli.cmd_provider()
    assert "deepseek-anthropic" not in providers.auth().get("providers", {})
    assert providers.active_model() == ""


def test_existing_provider_action_escape_preserves_everything(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = providers.auth_file().read_bytes()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(configuration, "choose_provider", lambda rows: 4)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B\x1b[B\x1b")
        assert not cli.cmd_provider()
    assert providers.auth_file().read_bytes() == before


@pytest.mark.parametrize("active", ["deepseek-anthropic", "openai"])
def test_clear_only_removes_selected_provider(configured: None, active: str) -> None:
    providers.save_auth("openai", "https://other.example", "other-key", ["other-model"])
    providers.use_model(
        active, "deepseek-flash" if active == "deepseek-anthropic" else "other-model"
    )
    other = providers.auth()["providers"]["openai"].copy()
    ledger.catalog_path().write_text('{"saved-model": {"provider": "deepseek", "input": 1}}')
    prices = ledger.catalog_path().read_bytes()
    result = CliRunner().invoke(cli.app, ["provider"], input=provider_keys("3\n"))
    assert result.exit_code == 0 and "配置已清除" in result.output, result.output
    assert providers.auth()["providers"] == {"openai": other}
    assert providers.configured_models() == [("openai", "other-model")]
    assert providers.active_model() == (
        "" if active == "deepseek-anthropic" else "openai/other-model"
    )
    assert ledger.catalog_path().read_bytes() == prices
    assert providers.auth_file().stat().st_mode & 0o777 == 0o600


def test_provider_edit_preserves_key_on_enter(configured: None) -> None:
    result = CliRunner().invoke(
        cli.app, ["provider"], input=provider_keys("2\nhttps://edited.example\n\ncustom-model\n")
    )
    assert result.exit_code == 0, result.output
    assert providers.auth()["providers"]["deepseek-anthropic"] == {
        "base_url": "https://edited.example",
        "api_key": "saved-secret",
        "models": ["custom-model"],
    }
