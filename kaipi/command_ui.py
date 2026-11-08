"""Temporary terminal command screens; conversation output stays on the primary screen."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app_session
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.widgets import Label, TextArea

from kaipi import cli, core, views
from kaipi.i18n import tr

QUERIES = {"help", "status", "version", "ledger", "raw"}
MENUS = {"provider", "model", "config", "statusline", "tree", "go", "resume", "rewind"}


@contextmanager
def temporary_screen() -> Iterator[None]:
    """Alternate screen preserves both the original cells and terminal scrollback."""
    output = get_app_session().output
    sys.stdout.flush()
    output.enter_alternate_screen()
    output.erase_screen()
    output.cursor_goto(0, 0)
    output.flush()
    try:
        yield
    finally:
        sys.stdout.flush()
        output.reset_attributes()
        output.quit_alternate_screen()
        output.show_cursor()
        output.flush()


def view(title: str, lines: list[str]) -> None:
    text = "".join(part for _, part, *_ in to_formatted_text(ANSI("\n".join(lines))))
    body = TextArea(text=text, read_only=True, scrollbar=True, wrap_lines=True)
    keys = KeyBindings()

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    @keys.add("c-d")
    def back(event: Any) -> None:
        event.app.exit()

    app: Application[None] = Application(
        layout=Layout(
            HSplit([Label(title), body, Label(tr("↑↓ / PgUp PgDn 浏览 · Esc 返回"))]),
            focused_element=body,
        ),
        key_bindings=keys,
        full_screen=True,
    )
    app.ttimeoutlen = 0.1
    app.run()


def wait_for_back() -> None:
    keys = KeyBindings()

    @keys.add("escape", eager=True)
    def back(event: Any) -> None:
        event.app.exit(result="")

    prompt: PromptSession[str] = PromptSession(key_bindings=keys, erase_when_done=True)
    prompt.app.ttimeoutlen = 0.1
    prompt.prompt(tr("Esc 或回车返回"))


def run(s: core.Session, argv: list[str]) -> bool:
    if not argv:
        return True
    name, *args = argv
    if name in {"new", "quit", "exit", "inspect-context"}:
        return cli.slash(s, argv)
    messages: list[str] = []
    try:
        if name in {"tree", "go", "rewind"} and not args and not s.log.state.nodes:
            view("/" + name, [tr("还没有可选择的节点")])
        elif name in QUERIES or (name == "trunk" and not args):
            view("/" + name, views.verb(s, argv) or [])
        elif (
            name in {"provider", "model", "config", "statusline", "update"}
            or (name in {"tree", "go", "resume"} and not args)
            or (name == "rewind" and len(args) <= 1)
        ):
            with temporary_screen():
                cli.slash(s, argv, emit=messages.extend)
                if name == "update":
                    wait_for_back()
        else:
            cli.slash(s, argv, emit=messages.extend)
    except (EOFError, KeyboardInterrupt, typer.Abort):
        # Cancellation belongs to this command, not to the enclosing conversation.
        return True
    if messages:
        cli.say([" · ".join(" ".join(message.splitlines()) for message in messages)])
    return True
