from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from string import Formatter
from typing import Any

import pytest
import typer
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from typer.testing import CliRunner

from kaipi import cli, configuration, context, core, i18n, providers, server, settings, tui, views
from tests.conftest import Builder


def plain(text: str | ANSI) -> str:
    return fragment_list_to_text(to_formatted_text(ANSI(text) if isinstance(text, str) else text))


def test_catalog_is_complete_and_parameters_match() -> None:
    catalog = i18n.messages()
    for key, translations in catalog.items():
        assert set(translations) == {"zh", "en"}, key
        fields = [
            {
                (name, spec, conversion)
                for _, name, spec, conversion in Formatter().parse(value)
                if name is not None
            }
            for value in translations.values()
        ]
        assert fields[0] == fields[1], key
    for path in Path(cli.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                assert node.args[0].value in catalog, (path.name, node.lineno, node.args[0].value)
    for _, args, description in views.COMMANDS:
        assert not args or args in catalog
        assert description in catalog
    for phase in tui.PHASES:
        assert phase in catalog


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_first_run_selects_language_before_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], lang: str
) -> None:
    monkeypatch.setattr(providers, "BUILTIN", {})
    monkeypatch.setattr(providers, "auth", lambda: {})
    monkeypatch.setattr(providers, "active_model", lambda: None)
    monkeypatch.delenv("KAIPI_MODEL", raising=False)
    monkeypatch.setattr(providers, "configured_models", lambda: [("provider", "model")])
    order: list[str] = []

    def choose(rows: list[str], **kwargs: Any) -> int:
        assert "Welcome to kaipi" in capsys.readouterr().out
        assert rows == ["简体中文", "English"]
        assert "Choose your language" in kwargs["title"]
        order.append("language")
        return list(i18n.LANGUAGES).index(lang)

    def provider() -> bool:
        assert i18n.language() == lang and settings.read()["language"] == lang
        order.append("provider")
        return True

    monkeypatch.setattr(configuration, "choose", choose)
    monkeypatch.setattr(cli, "cmd_provider", provider)

    def model() -> bool:
        order.append("model")
        return True

    monkeypatch.setattr(cli, "cmd_model", model)
    monkeypatch.setattr(cli, "clear_terminal", lambda: order.append("clear"))
    cli.onboard()
    assert order == ["language", "provider", "model", "clear"]
    shown = capsys.readouterr().out
    assert ("配置完成" if lang == "zh" else "Ready!") in shown
    if lang == "en":
        assert not re.search(r"[\u4e00-\u9fff]", shown)


def test_existing_install_keeps_chinese_without_language_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "auth", lambda: {"providers": {"openai": {}}})
    monkeypatch.setenv("KAIPI_MODEL", "openai/example")
    monkeypatch.setattr(configuration, "choose", lambda *a, **kw: pytest.fail("unexpected prompt"))
    cli.onboard()
    assert settings.read()["language"] == "zh"


def test_cancelled_language_choice_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "BUILTIN", {})
    monkeypatch.setattr(providers, "auth", lambda: {})

    def cancel(*args: Any, **kwargs: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(configuration, "choose", cancel)
    monkeypatch.setattr(cli, "cmd_provider", lambda: pytest.fail("provider before language"))
    with pytest.raises(typer.Exit):
        cli.onboard()
    assert not settings.path().exists()


@pytest.mark.parametrize("arrow", ["\x1b[C", "\x1b[D"])
def test_config_language_switch_updates_help_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrow: str
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    session = core.Session(tmp_path)
    completer = tui.SlashCompleter(session)
    assert "显示这份清单" in "\n".join(views.cmd_help(session))
    for lang in ["en", "zh"]:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("\x1b[B" + arrow + "\x1b")
            configuration.config()
        assert i18n.language() == lang
        completion = list(completer.get_completions(Document("/help"), CompleteEvent()))[0]
        assert completion.display_meta_text == (
            "Show this list" if lang == "en" else "显示这份清单"
        )
        assert ("context " if lang == "en" else "上下文 ") in views.status(session, 300)
        assert ("Show this list" if lang == "en" else "显示这份清单") in "\n".join(
            views.cmd_help(session)
        )


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_all_phases_keep_separators_fixed(tmp_path: Path, lang: str) -> None:
    settings.save({"language": lang})
    with create_app_session(output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))
        positions = []
        for phase in tui.PHASES:
            terminal.phase = phase
            shown = plain(terminal.toolbar())
            row = next(line for line in shown.splitlines() if "0.000s" in line)
            dots = [get_cwidth(row[: m.start()]) for m in re.finditer(" · ", row)]
            positions.append(dots)
            assert i18n.tr(phase) in row
        assert all(row == positions[0] for row in positions)


def test_language_does_not_change_model_context_or_history(tmp_path: Path) -> None:
    session = core.Session(tmp_path)
    builder = Builder(session.log)
    root = builder.node(None, "未知 /help", answer="等待模型")
    leaf = builder.node(root, "Explain context", answer="context 12.5%")
    builder.node(root, "unrelated branch")
    session.leaf = leaf
    before = session.log.path.read_bytes()
    requests = []
    for lang in ["zh", "en"]:
        settings.save({"language": lang})
        requests.append(context.build(session.log.state, session.leaf, [], "next input"))
        assert session.log.path.read_bytes() == before
        assert server.transcript(session.log.state.nodes[root])[0]["text"] == "未知 /help"
    assert requests[0] == requests[1]
    assert "unrelated branch" not in str(requests[1].messages)
    assert i18n.tr("错误：{error}", error="未知") == "Error: 未知"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_canvas_and_cli_help_use_selected_language(lang: str) -> None:
    settings.save({"language": lang})
    page = i18n.canvas_page(server.CANVAS_HTML.read_text()).decode()
    assert "{{html:" not in page and "{{js:" not in page
    assert ('lang="en"' if lang == "en" else 'lang="zh-CN"') in page
    assert ("Session returned to the terminal" if lang == "en" else "会话已移交到终端") in page
    help_output = CliRunner().invoke(cli.app, ["--help"])
    assert help_output.exit_code == 0, help_output.output
    assert ("start a session" if lang == "en" else "在当前目录开始新会话") in help_output.output
    assert (
        "Resume the most recent session" if lang == "en" else "接上最近的那个会话"
    ) in help_output.output


def test_first_language_picker_accepts_arrows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(providers, "BUILTIN", {})
    monkeypatch.setattr(providers, "auth", lambda: {})
    monkeypatch.setenv("KAIPI_MODEL", "openai/example")
    seen: list[str] = []

    def provider() -> bool:
        seen.append(i18n.language())
        return True

    monkeypatch.setattr(cli, "cmd_provider", provider)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B\r")
        cli.onboard()
    assert seen == ["en"]


def test_attachment_draft_translation_preserves_model_payload() -> None:
    import base64

    from kaipi.attachments import Attachment

    attachment = Attachment(name="notes.md", data=base64.b64encode("你好".encode()).decode())
    before = attachment.blocks()
    assert attachment.summary == "文本／文档 · 已提取内容"
    settings.save({"language": "en"})
    assert attachment.summary == "Text/document · extracted content"
    assert attachment.blocks() == before
    assert i18n.attachment_summary("GIF → PNG · 2 帧全部保留 · 帧时长(ms): 100,200") == (
        "GIF → PNG · all 2 frames kept · frame durations (ms): 100,200"
    )


def test_canvas_copy_is_escaped_in_its_source_context(monkeypatch: pytest.MonkeyPatch) -> None:
    settings.save({"language": "en"})
    malicious = "</script>'` ${alert(1)} &"
    monkeypatch.setitem(i18n.messages(), "test.copy", {"en": malicious, "zh": malicious})
    page = i18n.canvas_page(
        '<p>{{html:test.copy}}</p><script>const x = "{{js:test.copy}}";</script>'
    ).decode()
    assert "&lt;/script&gt;" in page and "&#x27;" in page
    script = page.split("<script>")[1]
    assert script.count("</script>") == 1
    assert "${alert" not in script and "`" not in script
