"""The terminal surface when stdin is a tty. The layout is the one people already know from
Claude Code and Kimi Code: everything the agent prints scrolls above, and a fixed area at the
bottom - a rule, the input line, a rule, the status bar - stays put. While a turn runs the
area stays, the bar says so, Esc stops the turn, and a line typed meanwhile is queued as the
next input. Typing `/` opens completion over the slash commands, with their usage; after a
command that takes a node id, over the session's nodes. Without a tty (a pipe, the tests)
`cli.interactive` uses a plain `input()` loop."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from kaipi import agent, cli, context
from kaipi.store import kaipi_dir

BUSY = "\033[33m⏳ 运行中 · Esc 停止\033[0m"


class SlashCompleter(Completer):
    """`/` and a prefix: the commands, each with its arguments as the meta column. A
    node-taking command followed by a space: the live nodes, newest first, each with the
    question that opened it, so an id is picked by what it was about rather than remembered."""

    def __init__(self, s: cli.Session) -> None:
        self.s = s

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        head, _, rest = text[1:].partition(" ")
        if not _:  # still typing the command itself
            for name, args, what in cli.COMMANDS:
                if name.startswith(head):
                    yield Completion(
                        f"/{name} " if args else f"/{name}",
                        start_position=-len(text),
                        display=f"/{name} {args}".strip(),
                        display_meta=what,
                    )
            return
        if head == "resume" and " " not in rest.strip():
            for p, line in cli.session_rows(self.s.cwd, self.s.log.path.name):
                sid = cli.short(p.stem)
                if sid.startswith(rest) or p.stem.startswith(rest):
                    yield Completion(
                        sid, start_position=-len(rest), display=sid, display_meta=line[8:]
                    )
            return
        if head in cli.NODE_ARG and " " not in rest.strip():
            state = self.s.log.state
            for n in sorted(state.nodes.values(), key=lambda n: n.seq, reverse=True):
                if n.status not in ("live", "archived"):
                    continue
                sid = state.label(n.id)
                if sid.lower().startswith(rest.lower()) or n.id.startswith(rest):
                    tag = "" if n.status == "live" else " [已归档]"
                    yield Completion(
                        sid,
                        start_position=-len(rest),
                        display=sid,
                        display_meta=(context.user_input(n)[:48] + tag) or "(root)",
                    )


class Terminal:
    def __init__(self, s: cli.Session) -> None:
        self.s = s
        self.running = False
        self.stop = threading.Event()
        self.done = threading.Event()
        kb = KeyBindings()

        @kb.add("escape", eager=True)
        def _esc(event: Any) -> None:
            if self.running:
                self.stop.set()

        @kb.add("c-c")
        def _ctrl_c(event: Any) -> None:
            if self.running:
                self.stop.set()
            else:
                event.app.exit(exception=KeyboardInterrupt())

        self.session: PromptSession[str] = PromptSession(
            message=self.message,
            history=FileHistory(str(kaipi_dir(s.cwd) / "history")),
            bottom_toolbar=self.toolbar,
            completer=SlashCompleter(s),
            complete_while_typing=True,
            reserve_space_for_menu=0,  # one line at rest; the menu makes room when it opens
            key_bindings=kb,
            # the bar carries its own colours; the default reverse video would fight them
            style=Style.from_dict({"bottom-toolbar": "noreverse"}),
            refresh_interval=0.5,
            # the prompt erases itself on Enter and kaipi echoes the line once, cleanly,
            # so the scrollback reads input / output / input rather than stale prompts
            erase_when_done=True,
        )

    @staticmethod
    def rule() -> str:
        try:
            cols = get_app().output.get_size().columns
        except Exception:  # noqa: BLE001 - no running app: a sane default
            cols = 80
        return f"{cli.DIM}{'─' * cols}{cli.RESET}"

    def label(self) -> str:
        return f"{self.s.name(self.s.leaf)}> "

    def message(self) -> ANSI:
        return ANSI(f"{self.rule()}\n{self.label()}")

    def toolbar(self) -> ANSI:
        if self.running and self.done.is_set():
            # the turn finished while the busy prompt was up: hand control back
            get_app().exit(result="")
        head = f"{BUSY}  " if self.running else ""
        return ANSI(f"{self.rule()}\n{head}{self.s.status()}")

    def echo(self, line: str) -> None:
        print(f"{cli.BOLD}{self.label()}{cli.RESET}{line}")

    def prompt(self) -> str:
        with patch_stdout(raw=True):
            line = self.session.prompt().strip()
        if line:
            self.echo(line)
        return line

    def turn(self, text: str, *, explore: bool = False) -> list[str]:
        """Run one turn in a worker thread, keeping the bottom area alive meanwhile.
        Returns the lines the user typed while it ran, in order."""
        outcome: dict[str, Any] = {}
        queued: list[str] = []

        def work() -> None:
            try:
                outcome["ok"] = cli.run_input(self.s, text, explore=explore, stop=self.stop)
            except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
                outcome["err"] = e
            finally:
                self.done.set()

        self.stop.clear()
        self.done.clear()
        self.running = True
        started = False

        def start() -> None:
            nonlocal started
            if not started:
                started = True
                threading.Thread(target=work, daemon=True).start()

        try:
            with patch_stdout(raw=True):
                while not self.done.is_set():
                    line = self.session.prompt(pre_run=start).strip()
                    if line:
                        queued.append(line)
                        print(f"{cli.DIM}已排队，这一轮结束后执行：{line}{cli.RESET}")
        finally:
            self.running = False
        if "err" in outcome:
            err = outcome["err"]
            if isinstance(err, agent.Interrupted):
                cli.after_interrupt(self.s, err)
                return queued
            raise err
        nid, dirty = outcome["ok"]
        cli.after_turn(self.s, nid, dirty)
        return queued


def loop(s: cli.Session) -> str:
    """The tty loop. Returns "canvas" when the user hands the session to the canvas."""
    t = Terminal(s)
    pending: list[str] = []
    while True:
        try:
            if pending:
                line = pending.pop(0)
                t.echo(line)
            else:
                line = t.prompt()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if not line:
            continue
        try:
            if line.startswith("/"):
                if line.split()[0] in ("/canvas", "/cli"):
                    s.save()
                    return "canvas"
                if line.split()[0] == "/explore":
                    pending = t.turn(line[len("/explore") :].strip(), explore=True) + pending
                elif not cli.slash(s, line[1:].split()):
                    return "quit"
            else:
                pending = t.turn(line) + pending
        except (typer.BadParameter, ValueError) as e:
            print(f"{cli.RED}{e}{cli.RESET}")
        s.save()
