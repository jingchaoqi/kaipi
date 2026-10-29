"""The terminal surface when stdin is a tty. The layout is the one people already know from
Claude Code and Kimi Code: everything the agent prints scrolls above, and a fixed area at the
bottom - a rule, the input line, a rule, the status bar - stays put. While a turn runs the
area stays, the bar says so, Esc stops the turn, and a line typed meanwhile is queued as the
next input. Typing `/` opens completion over the slash commands, with their usage; after a
command that takes a node id, over the session's nodes. Without a tty (a pipe, the tests)
`cli.interactive` uses a plain `input()` loop."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

import typer
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Label, RadioList

from kaipi import agent, cli, context
from kaipi.store import kaipi_dir

FPS = 60


def pick_rewind(s: cli.Session) -> str | None:
    rows = cli.tree_rows(s)
    if not rows:
        return None
    nodes = RadioList(
        values=[(nid, ANSI(line)) for nid, line in rows],
        default=s.leaf,
        select_on_focus=True,
        show_cursor=False,
    )
    # RadioList's default checks a value but leaves the keyboard cursor on row 0.
    nodes._selected_index = next((i for i, (nid, _) in enumerate(rows) if nid == s.leaf), 0)
    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def _accept(event: Any) -> None:
        event.app.exit(result=rows[nodes._selected_index][0])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    # Widget bindings take precedence over Application bindings. Attach these last
    # on the focused control so Enter submits instead of merely checking a radio.
    nodes.control.key_bindings = merge_key_bindings(
        [nodes.control.key_bindings or KeyBindings(), kb]
    )
    app: Application[str | None] = Application(
        layout=Layout(
            HSplit(
                [
                    Label("回退到哪个节点？  ↑/↓ 切换 · Enter 选择 · Esc 取消"),
                    nodes,
                    Label("* 主干  > 当前节点；选择后预览文件，再确认回退方式"),
                ]
            ),
            focused_element=nodes,
        ),
        key_bindings=kb,
        style=Style.from_dict({"radio-selected": "bg:#30343b bold"}),
        full_screen=False,
        erase_when_done=True,
    )
    try:
        return app.run()
    except (EOFError, KeyboardInterrupt):
        return None


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
        if not text.startswith("/") or len(text) < 2 or not text[1].isalpha():
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
        self.phase = "准备中"
        self.started_at = 0.0
        self._bar = ""
        self._bar_at = 0.0
        self._bar_width = 0
        self.stop = threading.Event()
        self.done = threading.Event()
        self.completer = SlashCompleter(s)
        self._selecting = False
        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event: Any) -> None:
            buffer = event.current_buffer
            state = buffer.complete_state
            if state and state.current_completion:
                # Selecting a command submits it; arguments still require an explicit
                # Tab. Arrow navigation itself leaves the user's prefix untouched.
                if not any(c.isspace() for c in state.original_document.text):
                    self._selecting = True
                    try:
                        buffer.apply_completion(state.current_completion)
                    finally:
                        self._selecting = False
            buffer.validate_and_handle()

        has_candidates = Condition(lambda: self.session.default_buffer.complete_state is not None)

        @kb.add("up", filter=has_candidates)
        @kb.add("down", filter=has_candidates)
        def _choose(event: Any) -> None:
            buffer = event.current_buffer
            state = buffer.complete_state
            if state:
                step = -1 if event.key_sequence[-1].key == "up" else 1
                index = (state.complete_index or 0) + step
                state.go_to_index(max(0, min(len(state.completions) - 1, index)))
                buffer.on_completions_changed.fire()

        @kb.add("tab", filter=has_candidates)
        def _tab(event: Any) -> None:
            buffer = event.current_buffer
            state = buffer.complete_state
            if state:
                index = state.complete_index or 0
                if buffer.document != state.original_document:
                    index = (index + 1) % len(state.completions)
                self._selecting = True
                try:
                    buffer.go_to_completion(index)
                finally:
                    self._selecting = False

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
            completer=self.completer,
            complete_while_typing=False,
            reserve_space_for_menu=0,  # one line at rest; the menu makes room when it opens
            key_bindings=kb,
            # the bar carries its own colours; the default reverse video would fight them
            style=Style.from_dict({"bottom-toolbar": "noreverse"}),
            refresh_interval=0,  # a timer is needed only while a turn runs
            # the prompt erases itself on Enter and kaipi echoes the line once, cleanly,
            # so the scrollback reads input / output / input rather than stale prompts
            erase_when_done=True,
        )
        self.session.default_buffer.on_text_changed += self.refresh_completions
        self.session.default_buffer.on_cursor_position_changed += self.refresh_completions

    def refresh_completions(self, buffer: Buffer) -> None:
        if self._selecting:
            return
        candidates = (
            list(self.completer.get_completions(buffer.document, CompleteEvent()))
            if buffer.cursor_position == len(buffer.text)
            else []
        )
        buffer.complete_state = (
            CompletionState(buffer.document, candidates, complete_index=0) if candidates else None
        )
        buffer.on_completions_changed.fire()

    def columns(self) -> int:
        try:
            return int(self.session.output.get_size().columns)
        except Exception:  # noqa: BLE001 - no running app: a sane default
            return 80

    def rule(self) -> str:
        return f"{cli.DIM}{'─' * self.columns()}{cli.RESET}"

    def label(self) -> str:
        return f"{self.s.name(self.s.leaf)}> "

    def message(self) -> ANSI:
        return ANSI(f"{self.rule()}\n{self.label()}")

    def toolbar(self) -> ANSI:
        if self.running and self.done.is_set():
            # the turn finished while the busy prompt was up: hand control back
            get_app().exit(result="")
        now, width = time.monotonic(), self.columns()
        # The ledger folds are much slower than drawing a frame. Keep their refresh
        # separate from the animation, and always refresh after a turn or resize.
        if not self.running or now - self._bar_at >= 0.5 or width != self._bar_width:
            self._bar = self.s.status(width)
            self._bar_at, self._bar_width = now, width
        if not self.running:
            return ANSI(f"{self.rule()}\n{self._bar}")
        elapsed = max(0.0, now - self.started_at)
        head = (elapsed * 8) % 12
        track = ""
        for i in range(12):
            distance = (head - i) % 12
            if distance > 6:
                distance -= 12
            glow = max(0.0, 1 - distance / 4 if distance >= 0 else 1 + distance)
            r, g, b = int(45 + 100 * glow), int(65 + 180 * glow), int(75 + 180 * glow)
            track += f"\033[38;2;{r};{g};{b}m•{cli.RESET}"
        phase = "正在停止" if self.stop.is_set() else self.phase
        sweep = (elapsed * 7) % (len(phase) + 6) - 3
        shimmer = ""
        for i, char in enumerate(phase):
            glow = max(0.0, 1 - abs(i - sweep) / 3)
            r, g, b = int(125 + 110 * glow), int(145 + 110 * glow), int(155 + 100 * glow)
            shimmer += f"\033[38;2;{r};{g};{b}m{char}{cli.RESET}"
        activity = f"{track}  {shimmer} · {elapsed:.0f}s · Esc 停止"
        return ANSI(f"{self.rule()}\n{activity}\n{self._bar}")

    def hook(self, kind: str, text: str) -> None:
        if kind == "phase":
            self.phase = text
        else:
            self.phase = {
                "cmd": "执行命令",
                "out": "处理命令结果",
                "wait": "等待重试",
                "text": "整理回答",
                "ledger": "整理结果",
            }.get(kind, self.phase)
            cli.print_hook(kind, text)

    def echo(self, line: str) -> None:
        if line.startswith("/") and line.split()[0] != "/explore":
            print(f"{cli.BOLD}{self.label()}{cli.RESET}{line}")
            return
        width = max(1, self.columns() - 1)
        rows: list[str] = []
        row, used = "", 0
        for char in (f"  {self.label()}{line}  ").expandtabs(4):
            size = get_cwidth(char)
            if char == "\n" or used + size > width:
                rows.append(row + " " * (width - used))
                row, used = "", 0
            if char != "\n" and size <= width:
                row += char
                used += size
        rows.append(row + " " * (width - used))
        panel = "\n".join([" " * width, *rows, " " * width])
        print_formatted_text([("", "\n"), ("bg:#30343b fg:#f3f4f6", panel), ("", "\n")])

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
                outcome["ok"] = cli.run_input(
                    self.s, text, hook=self.hook, explore=explore, stop=self.stop
                )
            except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
                outcome["err"] = e
            finally:
                self.done.set()

        self.stop.clear()
        self.done.clear()
        self.running = True
        self.phase = "准备中"
        self.started_at = time.monotonic()
        self._bar_at = 0.0
        self.session.refresh_interval = 1 / FPS
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
            self.session.refresh_interval = 0
        if "err" in outcome:
            err = outcome["err"]
            if isinstance(err, agent.Interrupted):
                cli.after_interrupt(self.s, err)
                return queued
            if isinstance(err, agent.Failed):
                cli.after_failure(self.s, err)
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
