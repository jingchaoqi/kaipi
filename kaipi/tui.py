"""The terminal surface when stdin is a tty. The layout is the one people already know from
Claude Code and Kimi Code: everything the agent prints scrolls above, and a fixed area at the
bottom - a rule, the input line, a rule, the status bar - stays put. While a turn runs the
area stays, the bar says so, Esc stops the turn, and a line typed meanwhile is queued as the
next input. Typing `/` opens completion over the slash commands, with their usage; after a
command that takes a node id, over the session's nodes. Without a tty (a pipe, the tests)
`cli.interactive` uses a plain `input()` loop."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable
from functools import partial
from io import StringIO
from queue import Empty, SimpleQueue
from typing import Any

import typer
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Label, RadioList
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding

from kaipi import (
    agent,
    attachments,
    cli,
    command_ui,
    context,
    core,
    graph,
    history,
    ledger,
    update,
    views,
)
from kaipi.i18n import tr
from kaipi.store import kaipi_dir

FPS = 60
FADE_SECONDS = 0.5
IDLE_COLOR = (112, 122, 130)
PHASES = (
    "就绪",
    "准备中",
    "等待模型",
    "执行命令",
    "处理结果",
    "等待重试",
    "整理回答",
    "整理结果",
    "生成标题",
    "正在停止",
    "已完成",
    "已停止",
    "未完成",
)
PHASE_WIDTH = 8  # Chinese uses four full-width characters; English is measured at render time.


def markdown(text: str, width: int, *, color: bool = True, indent: int = 0) -> str:
    stream = StringIO()
    Console(
        file=stream,
        width=max(1, width),
        force_terminal=color,
        color_system="truecolor" if color else None,
    ).print(
        Padding(
            Markdown(text, code_theme="monokai", inline_code_theme="ansi_dark"),
            (0, 0, 0, indent),
        ),
        end="",
    )
    return stream.getvalue()


def short_line(text: str, width: int, *, collapse: bool = True) -> str:
    if collapse:
        text = " ".join(text.split())
    text = "".join(c for c in text if c.isprintable())
    if get_cwidth(text) <= width:
        return text
    out, used = "", 0
    for char in text:
        size = get_cwidth(char)
        if used + size > width - 1:
            break
        out += char
        used += size
    return out + "…"


def command_summary(command: str) -> str:
    """An action label when the model supplied no explanation, not invented reasoning."""
    program = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    if program in {"cat", "sed", "head", "tail", "rg", "grep", "find", "ls", "pwd", "wc"}:
        return tr("查看项目文件与代码")
    if program in {"pytest", "ruff", "mypy", "uv", "python", "python3", "npm", "pnpm", "make"}:
        return tr("运行项目命令与检查")
    if program == "git":
        return tr("执行 Git 操作")
    return tr("执行命令")


def pick_node(s: core.Session, *, rewind: bool = False) -> str | None:
    """Browse the same tree for /tree, /go and /rewind; only Enter commits a choice."""
    rows = views.tree_rows(s, compact=True, live_only=not rewind)
    if not rows:
        print(tr("还没有可选择的节点"))
        return None
    st = s.log.state
    ids = [nid for nid, _ in rows]
    index = ids.index(s.leaf) if s.leaf in ids else len(ids) - 1
    trunk = graph.trunk(st)
    trunk_ids = {n.id for n in graph.lineage(st, trunk)} if trunk else set()
    remembered: dict[str, str] = {}
    kb = KeyBindings()

    def select(nid: str) -> None:
        nonlocal index
        if nid in ids:
            index = ids.index(nid)

    @kb.add("up")
    @kb.add("down")
    def vertical(event: Any) -> None:
        nonlocal index
        step = -1 if event.key_sequence[-1].key == "up" else 1
        index = max(0, min(len(rows) - 1, index + step))

    @kb.add("left")
    def parent(event: Any) -> None:
        nid = ids[index]
        pid = st.nodes[nid].parent_id
        if pid in ids:
            remembered[pid] = nid
            select(pid)

    @kb.add("right")
    def child(event: Any) -> None:
        nid = ids[index]
        children = [n for n in graph.children(st, nid, live_only=False) if n.id in ids]
        if children:
            target = remembered.get(nid) or next(
                (n.id for n in children if n.id in trunk_ids), children[-1].id
            )
            select(target)

    @kb.add("enter", eager=True)
    def accept(event: Any) -> None:
        event.app.exit(result=ids[index])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def cancel(event: Any) -> None:
        event.app.exit(result=None)

    def content() -> FormattedText:
        width = max(1, get_app().output.get_size().columns - 1)
        fragments = []
        for i, (_, line) in enumerate(rows):
            text = short_line(
                "".join(t for _, t, *_ in to_formatted_text(ANSI(line))),
                max(1, width - 2),
                collapse=False,
            )
            fragments.append(("", ("▸ " if i == index else "  ") + text))
            if i < len(rows) - 1:
                fragments.append(("", "\n"))
        return FormattedText(fragments)

    def details() -> str:
        n = st.nodes[ids[index]]
        width = max(1, get_app().output.get_size().columns - 1)
        own = ledger.fmt(ledger.own_tokens(st, n.id))
        cost = s.pricing.price(n.model).cost(n.usage)
        grafts = ", ".join(s.name(g.src_id) for g in n.grafts) or tr("无")
        role = tr("主线") if n.id == trunk else tr("探索分支：下一句从此分叉")
        action = tr("选择后预览文件并确认回退方式") if rewind else role
        return "\n".join(
            short_line(line, width)
            for line in [
                f"{s.name(n.id)} · {context.user_input(n)}",
                tr(
                    "{own} tokens · ${cost:.4f} · 嫁接：{grafts}", own=own, cost=cost, grafts=grafts
                ),
                action,
            ]
        )

    control = FormattedTextControl(
        content,
        focusable=True,
        key_bindings=kb,
        get_cursor_position=lambda: Point(x=0, y=index),
        show_cursor=False,
    )
    app: Application[str | None] = Application(
        layout=Layout(
            HSplit(
                [
                    Label(tr("回退到节点") if rewind else tr("会话树 · 选择继续的位置")),
                    Window(
                        control, height=Dimension(max=10), dont_extend_height=True, cursorline=True
                    ),
                    Window(height=1),
                    Label(details),
                    Label(
                        tr("↑↓ 浏览 · ←父节点 →子节点 · Enter ")
                        + (tr("选择") if rewind else tr("从此继续"))
                        + tr(" · Esc 取消")
                    ),
                ]
            ),
            focused_element=control,
        ),
        style=Style.from_dict({"cursor-line": "bg:#30343b fg:#f3f4f6"}),
        full_screen=False,
        erase_when_done=True,
    )
    try:
        return app.run()
    except (EOFError, KeyboardInterrupt):
        return None


def pick_rewind(s: core.Session) -> str | None:
    return pick_node(s, rewind=True)


def pick_session(s: core.Session) -> str | None:
    """An inline, scrolling session menu; opening or cancelling it changes nothing."""
    rows = views.session_rows(s.cwd, s.log.path.name)
    if not rows:
        print(tr("这个目录里还没有对话"))
        return None

    def label(line: str) -> str:
        return short_line(line, max(1, get_app().output.get_size().columns - 5))

    choices: RadioList[str] = RadioList(
        [(p.stem, partial(label, line)) for p, line in rows],
        default=s.log.path.stem,
        select_on_focus=True,
        open_character="",
        select_character="▸",
        close_character="",
        show_cursor=False,
    )
    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def accept(event: Any) -> None:
        event.app.exit(result=choices.current_value)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def cancel(event: Any) -> None:
        event.app.exit(result=None)

    app: Application[str | None] = Application(
        layout=Layout(
            HSplit(
                [
                    Label(tr("这个目录里的对话")),
                    HSplit([choices], height=Dimension(max=10)),
                    Label(tr("↑↓ 选择 · Enter 进入 · Esc 取消")),
                ]
            ),
            focused_element=choices,
        ),
        key_bindings=kb,
        style=Style.from_dict({"radio-selected": "bg:#30343b fg:#f3f4f6 bold"}),
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

    def __init__(self, s: core.Session) -> None:
        self.s = s

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or (len(text) > 1 and not text[1].isalpha()):
            return
        head, _, rest = text[1:].partition(" ")
        if not _:  # still typing the command itself
            commands = (
                sorted(views.COMMANDS, key=lambda command: command[0] != "help")
                if text == "/"
                else views.COMMANDS
            )
            for name, args, what in commands:
                if name.startswith(head):
                    yield Completion(
                        f"/{name} " if args else f"/{name}",
                        start_position=-len(text),
                        display=f"/{name} {tr(args)}".strip(),
                        display_meta=tr(what),
                    )
            return
        if head == "resume" and " " not in rest.strip():
            for p, line in views.session_rows(self.s.cwd, self.s.log.path.name):
                sid = views.short(p.stem)
                if sid.startswith(rest) or p.stem.startswith(rest):
                    yield Completion(
                        sid, start_position=-len(rest), display=sid, display_meta=line[8:]
                    )
            return
        if head in views.NODE_ARG and " " not in rest.strip():
            state = self.s.log.state
            for n in sorted(state.nodes.values(), key=lambda n: n.seq, reverse=True):
                if n.status not in ("live", "archived"):
                    continue
                sid = state.label(n.id)
                if sid.lower().startswith(rest.lower()) or n.id.startswith(rest):
                    tag = "" if n.status == "live" else tr(" [已归档]")
                    yield Completion(
                        sid,
                        start_position=-len(rest),
                        display=sid,
                        display_meta=(context.user_input(n)[:48] + tag) or "(root)",
                    )


class Terminal:
    def __init__(self, s: core.Session) -> None:
        self.s = s
        self.running = False
        self.phase = "就绪"
        self.started_at = 0.0
        self.finished_at: float | None = None
        self._fade_started_at: float | None = None
        self._track_colors = [(45, 65, 75)] * 12
        self._text_colors = [(125, 145, 155)]
        self._bar = ""
        self._bar_at = 0.0
        self._bar_width = 0
        self._text: list[str] = []
        self._intent = ""
        self._tool_section = False
        self._output: SimpleQueue[str] | None = None
        self.stop = threading.Event()
        self.done = threading.Event()
        self.completer = SlashCompleter(s)
        self._selecting = False
        self.attachments: dict[str, attachments.Attachment] = {}
        self._attachment_seq = {"image": 0, "file": 0}
        self.notice = ""
        self._exit_requested_at: float | None = None
        self._pasting = False
        self._submit_after_paste = False
        paste_jobs: list[
            tuple[Callable[[], tuple[list[attachments.Attachment], str]], str | None]
        ] = []
        kb = KeyBindings()

        def start_paste(
            event: Any,
            load: Callable[[], tuple[list[attachments.Attachment], str]],
            replace: str | None = None,
        ) -> None:
            paste_jobs.append((load, replace))
            if self._pasting:
                return
            self._pasting = True
            self._cancel_paste = False
            self.notice = tr("正在读取附件…")

            async def read() -> None:
                ok = True
                failure = ""
                try:
                    while paste_jobs:
                        job, original = paste_jobs.pop(0)
                        try:
                            items, text = await asyncio.to_thread(job)
                            if self._cancel_paste:
                                paste_jobs.clear()
                                self.notice = tr("已取消添加附件")
                                break
                            if items:
                                buffer = self.session.default_buffer
                                if original is not None and buffer.text == original:
                                    buffer.text = ""
                                self.add_attachments(items)
                            elif text:
                                self.paste_text(text)
                                if self.notice and not self.notice.endswith(
                                    tr(" · 回车发送 · 退格移除")
                                ):
                                    failure = self.notice
                                    ok = False
                            else:
                                failure = tr("剪贴板为空")
                                ok = False
                        except (ValueError, OSError) as e:
                            failure = str(e)
                            ok = False
                finally:
                    if failure:
                        self.notice = failure
                    self._pasting = False
                    submit, self._submit_after_paste = self._submit_after_paste, False
                    event.app.invalidate()
                if submit and ok:
                    _enter(event)

            event.app.create_background_task(read())

        @kb.add(Keys.BracketedPaste)
        def _paste(event: Any) -> None:
            paths = attachments.pasted_paths(event.data)
            if paths:
                start_paste(event, lambda: ([attachments.Attachment.read(p) for p in paths], ""))
            else:
                self.paste_text(event.data)

        @kb.add("c-v", eager=True)
        def _clipboard(event: Any) -> None:
            start_paste(event, attachments.clipboard)

        @kb.add("backspace")
        def _backspace(event: Any) -> None:
            if self._pasting:
                self._cancel_paste = True
                return
            buffer = event.current_buffer
            marker = next(
                (m for m in self.attachments if buffer.document.text_before_cursor.endswith(m)), ""
            )
            if marker:
                buffer.delete_before_cursor(len(marker))
                self.attachments.pop(marker)
            else:
                buffer.delete_before_cursor()

        @kb.add("enter")
        def _enter(event: Any) -> None:
            if self._pasting:
                self._submit_after_paste = True
                return
            buffer = event.current_buffer
            # Some terminal emulators insert dragged file paths without bracketed paste.
            # An entire file selection still counts as an explicit attachment gesture.
            if not any(marker in buffer.text for marker in self.attachments):
                paths = attachments.pasted_paths(buffer.text)
                if paths:
                    start_paste(
                        event,
                        lambda: ([attachments.Attachment.read(p) for p in paths], ""),
                        replace=buffer.text,
                    )
                    self._submit_after_paste = True
                    return
            state = buffer.complete_state
            if state and state.current_completion:
                # Enter accepts the highlighted command or argument before submitting.
                # Arrow navigation itself leaves the user's prefix untouched.
                self._selecting = True
                try:
                    buffer.apply_completion(state.current_completion)
                finally:
                    self._selecting = False
            if (
                any(marker in buffer.text for marker in self.attachments)
                and buffer.text.startswith("/")
                and not buffer.text.startswith("/explore ")
            ):
                self.notice = tr("附件请随问题发送；也可使用 /explore 问题")
                return
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
                now = time.monotonic()
                if self._exit_requested_at is not None and now - self._exit_requested_at < 3:
                    event.app.exit(exception=KeyboardInterrupt())
                    return
                self._exit_requested_at = now
                self.notice = tr("再按一次 Ctrl+C 退出（3 秒内）；继续输入可取消")

                def expire() -> None:
                    if self._exit_requested_at == now:
                        self.cancel_exit_confirmation()
                        event.app.invalidate()

                event.app.loop.call_later(3, expire)

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
        # PromptSession normally gives the input window all remaining height. Keep
        # it compact and put spare space above it, so output cannot move the input.
        self.session.layout.current_window.dont_extend_height = Always()
        prompt_layout = self.session.layout.container
        floats: list[Float] = []
        for container in self.session.layout.walk():
            if isinstance(container, FloatContainer):
                floats.extend(f for f in container.floats if not f.ycursor)
                container.floats = []
        self.session.layout.container = FloatContainer(
            HSplit([Window(), CompletionsMenu(max_height=10, scroll_offset=1), prompt_layout]),
            floats,
        )
        self.session.default_buffer.on_text_changed += self.refresh_completions
        self.session.default_buffer.on_cursor_position_changed += self.refresh_completions

    def add_attachments(self, items: list[attachments.Attachment]) -> None:
        # Read/validate the whole batch first, so a bad file cannot leave a half-paste.
        buffer = self.session.default_buffer
        active = {m: a for m, a in self.attachments.items() if m in buffer.text}
        attachments.validate([*active.values(), *items])
        self.attachments = active
        for item in items:
            kind = item.kind
            self._attachment_seq[kind] += 1
            chip = f"[{kind}{self._attachment_seq[kind]}]"
            self.attachments[chip] = item
            buffer.insert_text(chip)
        self.notice = items[-1].name + " · " + items[-1].summary + tr(" · 回车发送 · 退格移除")

    def paste_text(self, text: str) -> None:
        self.notice = ""
        try:
            paths = attachments.pasted_paths(text)
            if paths:
                self.add_attachments([attachments.Attachment.read(path) for path in paths])
            else:
                self.session.default_buffer.insert_text(
                    text.replace("\r\n", "\n").replace("\r", "\n")
                )
        except (ValueError, OSError) as e:
            self.notice = str(e)

    def collect_input(self, text: str) -> str:
        markers = sorted((m for m in self.attachments if m in text), key=text.index)
        items = [self.attachments[m] for m in markers]
        if not text:
            return text  # a turn may finish while the next draft is still being edited
        self.attachments.clear()
        self._attachment_seq = {"image": 0, "file": 0}
        self.notice = ""
        return attachments.Input(text, items) if items else text

    def refresh_completions(self, buffer: Buffer) -> None:
        self.cancel_exit_confirmation()
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

    def cancel_exit_confirmation(self) -> None:
        if self._exit_requested_at is not None:
            self._exit_requested_at = None
            if self.notice.startswith(tr("再按一次 Ctrl+C")):
                self.notice = ""

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
        now, width = time.monotonic(), self.columns()
        # The ledger folds are much slower than drawing a frame. Keep their refresh
        # separate from the animation, and always refresh after a turn or resize.
        if not self._bar_at or now - self._bar_at >= 0.5 or width != self._bar_width:
            self._bar = views.status(self.s, width)
            self._bar_at, self._bar_width = now, width
        elapsed = (
            max(0.0, (self.finished_at if self.finished_at is not None else now) - self.started_at)
            if self.started_at
            else 0.0
        )
        phase = tr("正在停止" if self.running and self.stop.is_set() else self.phase)
        phase_width = max(get_cwidth(tr(label)) for label in PHASES)
        if self.running:
            head = (elapsed * 8) % 12
            self._track_colors = []
            for i in range(12):
                distance = (head - i) % 12
                if distance > 6:
                    distance -= 12
                glow = max(0.0, 1 - distance / 4 if distance >= 0 else 1 + distance)
                self._track_colors.append(
                    (int(45 + 100 * glow), int(65 + 180 * glow), int(75 + 180 * glow))
                )
            sweep = (elapsed * 7) % (len(phase) + 6) - 3
            self._text_colors = []
            for i in range(len(phase)):
                glow = max(0.0, 1 - abs(i - sweep) / 3)
                self._text_colors.append(
                    (int(125 + 110 * glow), int(145 + 110 * glow), int(155 + 100 * glow))
                )
        fade = (
            0.0
            if self.running
            else (
                min(1.0, max(0.0, (now - self._fade_started_at) / FADE_SECONDS))
                if self._fade_started_at is not None
                else 1.0
            )
        )
        fade = fade * fade * (3 - 2 * fade)  # smooth start and stop, with no sudden color change

        def paint(text: str, source: tuple[int, int, int]) -> str:
            r, g, b = (round(a + (z - a) * fade) for a, z in zip(source, IDLE_COLOR, strict=True))
            return f"\033[38;2;{r};{g};{b}m{text}{cli.RESET}"

        track = "".join(paint("•", color) for color in self._track_colors)
        shimmer = "".join(
            paint(char, self._text_colors[min(i, len(self._text_colors) - 1)])
            for i, char in enumerate(phase)
        )
        padding = max(0, phase_width - get_cwidth(phase))
        shimmer = " " * (padding // 2) + shimmer + " " * (padding - padding // 2)
        hint = self.notice or (tr("Esc 停止") if self.running else tr("等待输入"))
        reserved = 14 + phase_width + get_cwidth(f" · {elapsed:.3f}s · ")
        hint = short_line(hint, max(1, width - reserved - 1))
        tail = paint(f" · {elapsed:.3f}s · {hint}", (220, 229, 231))
        activity = f"{track}  {shimmer}{tail}"
        return ANSI(f"{self.rule()}\n{activity}\n{self._bar}")

    def fading(self) -> bool:
        return (
            not self.running
            and self._fade_started_at is not None
            and time.monotonic() - self._fade_started_at < FADE_SECONDS
        )

    async def fade_out(self, app: Application[str]) -> None:
        while self.fading():
            app.invalidate()
            await asyncio.sleep(1 / FPS)
        app.invalidate()

    def hook(self, kind: str, text: str) -> None:
        if kind == "phase":
            if text == "生成标题":
                self.flush_answer()
            self.phase = text
            self._intent = ""
        else:
            self.phase = {
                "cmd": "执行命令",
                "out": "处理结果",
                "wait": "等待重试",
                "text": "整理回答",
                "ledger": "整理结果",
            }.get(kind, self.phase)
            if kind == "text":
                # Text before a tool call is its explanation; text at the end of the
                # turn is the answer. Hold it until we know which one it is.
                self._text.append(text)
            elif kind == "cmd":
                self._tool_section = True
                if self._text:
                    self._intent = markdown("\n\n".join(self._text), 120, color=False)
                    self._text.clear()
                indent = min(2, max(0, self.columns() - 2))
                row_width = max(1, self.columns() - indent - 1)
                width = min(120, row_width)
                intent = self._intent or command_summary(text)
                for style, content in [
                    ("\033[38;2;177;188;186m", f"· {intent}"),
                    ("\033[38;2;147;159;158m", "$ " + text),
                ]:
                    line = short_line(content, width)
                    padding = " " * (row_width - get_cwidth(line))
                    self.write(
                        " " * indent + f"\033[48;2;59;66;64m{style}{line}{padding}{cli.RESET}"
                    )
            elif kind == "out":
                pass  # kept in the node; /canvas can expand the complete output
            else:
                if kind == "ledger":
                    self.flush_answer()
                    self.write("")
                cli.print_hook(kind, text, emit=self.write)
                if kind == "ledger":
                    self.write("")

    def write(self, text: str, end: str = "\n") -> None:
        if self._output is None:
            print(text, end=end)
        else:
            self._output.put(text + end)

    def drain_output(self) -> None:
        chunks: list[str] = []
        if self._output is not None:
            while True:
                try:
                    chunks.append(self._output.get_nowait())
                except Empty:
                    break
        if chunks:
            output = self.session.output
            output.enable_autowrap()
            output.write_raw("".join(chunks).replace("\n", "\r\n"))
            output.flush()

    def flush_answer(self) -> None:
        if self._text:
            text = "\n\n".join(self._text)
            self._text.clear()
            if self._tool_section:
                self.write("")
                self._tool_section = False
            self.write(
                markdown(text, self.columns() - 1, indent=min(2, max(0, self.columns() - 2))),
                end="",
            )

    def replay(self) -> None:
        def question(node: str, text: str) -> None:
            self._intent = ""
            self._tool_section = False
            self.echo(text, label=f"{node}> ")

        history.replay(self.s, question, self.hook)
        self.phase = "就绪"
        self._intent = ""
        self._tool_section = False

    def echo(self, line: str, *, label: str | None = None) -> None:
        if label is None and line.startswith("/") and line.split()[0] != "/explore":
            return
        width = max(1, self.columns() - 1)
        rows: list[str] = []
        row, used = "", 0
        for char in (f"  {label if label is not None else self.label()}{line}  ").expandtabs(4):
            size = get_cwidth(char)
            if char == "\n" or used + size > width:
                rows.append(row + " " * (width - used))
                row, used = "", 0
            if char != "\n" and size <= width:
                row += char
                used += size
        rows.append(row + " " * (width - used))
        panel = "\n".join([" " * width, *rows, " " * width])
        print_formatted_text(
            FormattedText([("", "\n"), ("bg:#30343b fg:#f3f4f6", panel), ("", "\n")])
        )

    def prompt(self, *, default: str = "") -> str:
        def start_fade() -> None:
            if self.fading():
                get_app().create_background_task(self.fade_out(get_app()))

        with patch_stdout(raw=True):
            line = self.collect_input(
                self.session.prompt(default=default, pre_run=start_fade).strip()
            )
        if line:
            self.echo(line)
        return line

    def turn(self, text: str, *, explore: bool = False, retry: bool = False) -> list[str]:
        """Run one turn in a worker thread, keeping the bottom area alive meanwhile.
        On normal completion the prompt stays alive until the next submission.
        Returns queued inputs, or that next submitted input, in order."""
        outcome: dict[str, Any] = {}
        queued: list[str] = []

        def work() -> None:
            try:
                outcome["ok"] = cli.run_input(
                    self.s, text, hook=self.hook, explore=explore, stop=self.stop, retry=retry
                )
            except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
                outcome["err"] = e
            finally:
                try:
                    self.flush_answer()
                finally:
                    self.done.set()

        self.stop.clear()
        self.done.clear()
        self.running = True
        self.phase = "准备中"
        self._text.clear()
        self._intent = ""
        self.started_at = time.monotonic()
        self._tool_section = False
        self.finished_at = None
        self._fade_started_at = None
        self._bar_at = 0.0
        self._output = SimpleQueue()
        started = False

        def finish() -> None:
            self.running = False
            self.finished_at = time.monotonic()
            self._fade_started_at = self.finished_at
            self._bar_at = 0.0
            err = outcome.get("err")
            self.phase = (
                "已完成"
                if "ok" in outcome
                else "已停止"
                if isinstance(err, agent.Interrupted) or self.stop.is_set()
                else "未完成"
            )

        async def redraw(app: Application[str]) -> None:
            while self.running or self.fading():
                await asyncio.sleep(1 / FPS)
                finished = self.running and self.done.is_set()
                if finished:
                    finish()
                if self._output is not None and not self._output.empty():
                    # Drain model output on the UI loop before switching to idle.
                    # One batch avoids separate hide/redraw cycles for each line.
                    output = self.session.output
                    output.write_raw("\033[?2026h")
                    output.flush()
                    try:
                        await run_in_terminal(self.drain_output)
                        # run_in_terminal redraws once before the terminal answers
                        # its cursor-position query. Keep that provisional layout
                        # hidden until the final available height is known.
                        if output.responds_to_cpr:
                            await app.renderer.wait_for_cpr_responses()
                        if finished:
                            self._fade_started_at = time.monotonic()
                        app._redraw()
                    finally:
                        output.write_raw("\033[?2026l")
                        output.flush()
                app.invalidate()
                if finished and (queued or "err" in outcome or outcome["ok"][1]):
                    app.exit(result="")

        def start() -> None:
            nonlocal started
            get_app().create_background_task(redraw(get_app()))
            if not started:
                started = True
                threading.Thread(target=work, daemon=True).start()

        try:
            with patch_stdout(raw=True):
                while True:
                    line = self.collect_input(self.session.prompt(pre_run=start).strip())
                    if line:
                        queued.append(line)
                    if self.done.is_set():
                        break
                    if line:
                        self.write(
                            tr(
                                "{v0}已排队，这一轮结束后执行：{line}{v2}",
                                v0=cli.DIM,
                                line=line,
                                v2=cli.RESET,
                            )
                        )
        finally:
            if self.finished_at is None:
                finish()
            self.drain_output()
            self._output = None
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


def loop(s: core.Session) -> str:
    """The tty loop. Returns "canvas" when the user hands the session to the canvas."""
    t = Terminal(s)
    if s.leaf:
        t.replay()
    update.background(lambda text: print(f"\n{cli.DIM}{text}{cli.RESET}\n"))
    pending: list[str] = []
    draft = ""
    while True:
        try:
            from_queue = bool(pending)
            if pending:
                line = pending.pop(0)
                t.echo(line)
            else:
                line = t.prompt(default=draft) if draft else t.prompt()
                draft = ""
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if not line:
            continue
        try:
            if line.startswith("/"):
                previous = s.log.path
                if line.split()[0] == "/update" and (
                    pending
                    or t.attachments
                    or t._pasting
                    or (from_queue and t.session.default_buffer.text.strip() not in ("", line))
                ):
                    if from_queue and t.session.default_buffer.text.strip() != line:
                        draft = t.session.default_buffer.text
                    print(tr("还有排队消息或附件草稿，请处理完后再输入 /update。"))
                    continue
                if line.split()[0] in ("/canvas", "/cli"):
                    return "canvas"
                if line.split()[0] == "/retry":
                    if len(line.split()) != 1:
                        raise ValueError(tr("用法：/retry（不带参数）"))
                    node = s.retry_node()
                    assert node.request is not None
                    t.echo(
                        str(attachments.Input.from_blocks(node.request.input)),
                        label=s.name(node.parent_id) + "> ",
                    )
                    pending = t.turn("", retry=True) + pending
                elif line.split()[0] == "/explore":
                    question = line[len("/explore") :].strip()
                    if isinstance(line, attachments.Input):
                        question = attachments.Input(question, line.attachments)
                    pending = t.turn(question, explore=True) + pending
                elif not command_ui.run(s, line[1:].split()):
                    return "quit"
                elif line.split()[0] == "/new":
                    pending.clear()
                    t = Terminal(s)
                elif s.log.path != previous:
                    cli.clear_terminal()
                    t = Terminal(s)
                    t.replay()
                elif line.split()[0] == "/statusline":
                    t._bar_at = 0.0
            else:
                pending = t.turn(line) + pending
        except (EOFError, KeyboardInterrupt):
            return "quit"
        except (typer.BadParameter, ValueError) as e:
            print(f"{cli.RED}{e}{cli.RESET}")
