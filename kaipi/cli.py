"""CLI (§8): sub-commands and the same verbs as slash commands inside the interactive loop."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from kaipi import agent, core, history, i18n, ledger, settings, update, views
from kaipi.i18n import tr
from kaipi.store import list_sessions


def _localize_help(command: Any, ctx: Any) -> None:
    command.help = tr(command.help or "")
    command.rich_help_panel = tr("命令")
    for parameter in command.get_params(ctx):
        if isinstance(parameter, (TyperOption, TyperArgument)):
            parameter.rich_help_panel = tr("选项" if isinstance(parameter, TyperOption) else "参数")
            if parameter.help:
                parameter.help = tr(parameter.help)


class LocalizedCommand(TyperCommand):
    def get_usage(self, ctx: Any) -> str:
        return super().get_usage(ctx).replace("Usage:", tr("用法："), 1)

    def format_help(self, ctx: Any, formatter: Any) -> None:
        _localize_help(self, ctx)
        super().format_help(ctx, formatter)


class LocalizedGroup(TyperGroup):
    """Translate help and domain errors at the CLI boundary."""

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ValueError as err:
            raise typer.BadParameter(str(err)) from err

    def get_usage(self, ctx: Any) -> str:
        return super().get_usage(ctx).replace("Usage:", tr("用法："), 1)

    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            _localize_help(command, ctx)
        return command

    def format_help(self, ctx: Any, formatter: Any) -> None:
        _localize_help(self, ctx)
        super().format_help(ctx, formatter)


app = typer.Typer(
    cls=LocalizedGroup, add_completion=False, no_args_is_help=False, invoke_without_command=True
)
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def clear_terminal() -> None:
    if sys.stdout.isatty():
        typer.echo("\033[2J\033[3J\033[H", nl=False)


def say(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


# --- surface lock: a session is open in exactly one place (CLI or canvas) at a time ----


def refuse_if_open(cwd: Path) -> None:
    h = core.lock_holder(cwd)
    if h is not None:
        where = (
            tr("the canvas at {v0}", v0=h["url"])
            if h.get("surface") == "canvas"
            else tr("another terminal")
        )
        typer.echo(
            tr(
                "{RED}session is open in {where} (pid {v2}); /quit there first{RESET}",
                RED=RED,
                where=where,
                v2=h["pid"],
                RESET=RESET,
            )
        )
        raise typer.Exit(1)


def cmd_provider(*, report: Callable[[str], None] = typer.echo) -> bool:
    from kaipi import configuration

    return configuration.run(lambda: configuration.provider(report=report))


def cmd_model(*, report: Callable[[str], None] = typer.echo) -> bool:
    from kaipi import configuration

    return configuration.run(lambda: configuration.model(report=report))


def pick_session(s: core.Session) -> str | None:
    """Use the keyboard menu on a tty, with a numbered fallback for plain input."""
    if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
        from kaipi.tui import pick_session as picker

        return picker(s)
    rows = views.session_rows(s.cwd, s.log.path.name)
    typer.echo(tr("{BOLD}这个目录里的对话{RESET}", BOLD=BOLD, RESET=RESET))
    for i, (_, line) in enumerate(rows, 1):
        typer.echo(f"  {i:>2}. {line}")
    pick = typer.prompt(tr("序号（回车取消）"), default=0, type=int, show_default=False)
    return rows[pick - 1][0].stem if 1 <= pick <= len(rows) else None


OUT_LINES = 4  # of a command's output, in the terminal


def preview(out: str, lines: int = OUT_LINES) -> str:
    """A command's output, shortened. Terminal scrollback cannot be folded open again once
    printed, so the terminal shows the head and says how much it kept back; the whole thing
    is in the node, and the canvas expands it on a click."""
    kept = out.rstrip("\n").split("\n")
    head = [ln[:200] for ln in kept[:lines]]
    if len(kept) > lines:
        head.append(tr("… 还有 {v0} 行（/canvas 里点开看全部）", v0=len(kept) - lines))
    return f"{DIM}" + "\n".join(head) + RESET


def print_hook(kind: str, t: str, *, emit: Callable[[str], None] = typer.echo) -> None:
    if kind == "text":
        emit(t)
    elif kind == "cmd":
        emit(f"{DIM}$ {t}{RESET}")
    elif kind == "out":
        emit(preview(t))
    elif kind == "guard":
        emit(tr("{RED}exploration branch dirtied the working tree{RESET}", RED=RED, RESET=RESET))
    elif kind == "stop":
        emit(f"{RED}stopped: {t}{RESET}")
    elif kind == "wait":
        emit(f"{DIM}⏳ {t}{RESET}")
    elif kind == "ledger":
        emit(f"{DIM}{t}{RESET}")


def run_input(
    s: core.Session,
    text: str,
    hook: agent.Hook = print_hook,
    *,
    explore: bool = False,
    stop: threading.Event | None = None,
    retry: bool = False,
) -> tuple[str, bool]:
    def emit(ev: core.Event) -> None:
        rendered = views.event(s, ev)
        if rendered:
            hook(*rendered)

    return core.run_input(s, text, emit, explore=explore, stop=stop, retry=retry)


class EscWatcher:
    """Esc during a turn means stop. The terminal is in line mode, so a keypress would not
    arrive until Enter; for the duration of the turn we read raw and watch for it in a
    thread. Anywhere without a tty (a pipe, Windows, a test) this does nothing and Ctrl-C
    remains the way out."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._done = threading.Event()
        self._saved: Any = None

    def __enter__(self) -> threading.Event:
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:  # noqa: BLE001 - no tty, no Esc; never a reason to fail a turn
            self._saved = None
            return self.stop
        threading.Thread(target=self._watch, daemon=True).start()
        return self.stop

    def _watch(self) -> None:
        import select

        while not self._done.is_set():
            if not select.select([sys.stdin], [], [], 0.1)[0] or sys.stdin.read(1) != "\x1b":
                continue
            # An arrow key, Home/End, an F-key and a bracketed paste all start with ESC.
            # A bare ESC is followed by nothing; anything else is a sequence to swallow.
            if select.select([sys.stdin], [], [], 0.05)[0]:
                while select.select([sys.stdin], [], [], 0.02)[0]:
                    sys.stdin.read(1)
                continue
            self.stop.set()
            return

    def __exit__(self, *_: object) -> None:
        self._done.set()
        if self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)


def retry_hint(s: core.Session) -> None:
    if s.can_retry:
        typer.echo(tr("输入 /retry 可重新运行上一轮。"))


def after_failure(s: core.Session, err: agent.Failed) -> None:
    """A provider that refused ends the turn like a stop does: the node is aborted, the
    tokens it burned are billed, and the cursor is back on the parent to try again."""
    typer.echo(tr("{RED}这一轮没能完成：{err}{RESET}", RED=RED, err=err, RESET=RESET))
    typer.echo(
        tr(
            "{DIM}已花掉的 token 记在账上；下一句从 {v1} 重新长出兄弟节点{RESET}",
            DIM=DIM,
            v1=s.name(s.leaf),
            RESET=RESET,
        )
    )

    retry_hint(s)


def after_interrupt(s: core.Session, stopped: agent.Interrupted) -> None:
    """What the terminal says and asks once a turn has been stopped. Asked after the turn,
    on the main thread, so it never collides with a prompt that is still up."""
    typer.echo(
        tr(
            "{RED}已停止。这一轮作废，不会进入之后的上下文；花掉的 token 已记账。{RESET}",
            RED=RED,
            RESET=RESET,
        )
    )
    if s.log.state.nodes[stopped.node_id].guard_dirty and typer.confirm(
        tr("被停掉的探索改动了工作树，回滚吗（git checkout -- . && git clean -fd）?"), default=False
    ):
        s.reset()
    typer.echo(
        tr(
            "{DIM}下一句会从 {v1} 重新长出一个兄弟节点{RESET}",
            DIM=DIM,
            v1=s.name(s.leaf),
            RESET=RESET,
        )
    )

    retry_hint(s)


def after_turn(s: core.Session, nid: str, dirty: bool) -> None:
    if dirty:
        if typer.confirm(
            tr("revert working tree (git checkout -- . && git clean -fd)?"), default=False
        ):
            s.reset()
        elif typer.confirm(tr("pin trunk to {v0} instead?", v0=s.name(nid)), default=False):
            say(views.cmd_trunk(s, nid))


def cli_turn(s: core.Session, text: str, *, explore: bool = False, retry: bool = False) -> None:
    try:
        with EscWatcher() as stop:
            nid, dirty = run_input(s, text, explore=explore, stop=stop, retry=retry)
    except agent.Interrupted as stopped:
        after_interrupt(s, stopped)
        return
    except agent.Failed as err:
        after_failure(s, err)
        return
    after_turn(s, nid, dirty)


def interactive(s: core.Session) -> str:
    """The terminal loop. Returns "canvas" when the user hands the session to the canvas.
    On a tty this is the fixed-bottom layout in `kaipi.tui`; anywhere else (a pipe, the
    tests) a plain line loop that prints the status bar once."""
    core.set_lock(s.cwd, "cli")
    typer.echo(f"{DIM}{tr(views.HELP)}{RESET}")
    if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
        from kaipi import tui

        return tui.loop(s)
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    typer.echo(f"kaipi  {views.status(s)}")
    history.replay(s, lambda node, text: typer.echo(f"{node}> {text}"), print_hook)
    exit_requested: float | None = None
    while True:
        try:
            line = input(f"{s.name(s.leaf)}> ").strip()
        except EOFError:
            return "quit"
        except KeyboardInterrupt:
            now = time.monotonic()
            if exit_requested is not None and now - exit_requested < 3:
                return "quit"
            exit_requested = now
            typer.echo(tr("\n再按一次 Ctrl+C 退出（3 秒内）；继续输入可取消"))
            continue
        exit_requested = None
        if not line:
            continue
        try:
            if line.startswith("/"):
                previous = s.log.path
                if line.split()[0] in ("/canvas", "/cli"):
                    return "canvas"
                if not slash(s, line[1:].split()):
                    return "quit"
                if s.log.path != previous:
                    history.replay(s, lambda node, text: typer.echo(f"{node}> {text}"), print_hook)
            else:
                cli_turn(s, line)
        except (typer.BadParameter, ValueError) as e:
            typer.echo(f"{RED}{e}{RESET}")


def run_surfaces(s: core.Session, surface: str) -> None:
    """Alternate between the two surfaces until the user quits. Exactly one is open."""
    from kaipi.server import serve

    upgrade: update.Plan | None = None
    try:
        while True:
            if surface == "canvas":
                url = serve(s, int(os.environ.get("KAIPI_PORT", "0")))
                clear_terminal()
                typer.echo(tr("canvas at {url} closed; session is back in this terminal", url=url))
                surface = "cli"
            else:
                if interactive(s) != "canvas":
                    return
                surface = "canvas"
                typer.echo(
                    tr(
                        "{DIM}canvas opened in your browser; this prompt is closed "
                        "until the canvas sends /cli (or press Ctrl-C here){RESET}",
                        DIM=DIM,
                        RESET=RESET,
                    )
                )
    except update.Ready as ready:
        upgrade = ready.plan
    finally:
        s.save()
        core.release_lock(s.cwd)
        if not s.log.state.nodes and upgrade is None:
            s.log.path.unlink(missing_ok=True)
        if upgrade is None:
            clear_terminal()
            typer.echo(tr("\n  {DIM}已关闭会话{RESET}\n", DIM=DIM, RESET=RESET))
            st = s.log.state
            if st.nodes:
                title = " ".join(core.session_title(st).split())
                directory = views._home(s.cwd, limit=len(str(s.cwd)))
                typer.echo(tr("  {DIM}会话详情：{RESET}", DIM=DIM, RESET=RESET))
                typer.echo(
                    tr(
                        "  {title} | {directory} | {v2} 个节点 | ${v3:.4f}\n",
                        title=title,
                        directory=directory,
                        v2=len(st.nodes),
                        v3=ledger.total_cost(st, s.pricing),
                    )
                )
                typer.echo(
                    tr(
                        "  {DIM}恢复此会话：{RESET}\n  {GREEN}kaipi -r {v3}{v4}\n",
                        DIM=DIM,
                        RESET=RESET,
                        GREEN=GREEN,
                        v3=s.log.path.stem,
                        v4=RESET,
                    )
                )
            else:
                typer.echo(
                    tr("  {DIM}本次尚无对话。{RESET}\n\n  重新开始：kaipi\n", DIM=DIM, RESET=RESET)
                )
    if upgrade:
        update.install(upgrade, resume=s.log.path.stem)


def slash(s: core.Session, argv: list[str], *, emit: Callable[[list[str]], None] = say) -> bool:
    """A slash command in the terminal. False means quit."""
    if not argv:
        return True
    name, args = argv[0], argv[1:]
    match name:
        case "quit" | "exit":
            return False
        case "new":
            out = views.verb(s, argv)
            clear_terminal()
            emit(out or [])
        case "provider":
            if cmd_provider(report=lambda text: emit([text])):
                s.refresh_configuration()
        case "model":
            cmd_model(report=lambda text: emit([text]))
        case "config":
            if args:
                raise typer.BadParameter(tr("用法：/config（不带参数）"))
            config()
        case "statusline":
            if args:
                raise typer.BadParameter(tr("用法：/statusline（不带参数）"))
            statusline()
        case "update":
            if args:
                raise typer.BadParameter(tr("用法：/update（不带参数）"))
            plan = update.prepare(typer.echo)
            if plan:
                raise update.Ready(plan)
        case "retry":
            if args:
                raise ValueError(tr("用法：/retry（不带参数）"))
            cli_turn(s, "", retry=True)
        case "explore":
            cli_turn(s, " ".join(args), explore=True)
        case "resume" if not args:
            picked = pick_session(s)
            if picked:
                emit(views.cmd_resume(s, picked))
        case "tree" | "go" if not args:
            if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
                from kaipi.tui import pick_node

                ref = pick_node(s)
                if ref:
                    emit(views.cmd_go(s, ref))
            else:
                emit(views.cmd_tree(s))
                if name == "go" and s.log.state.nodes:
                    ref = typer.prompt(tr("目标节点（回车取消）"), default="", show_default=False)
                    if ref:
                        emit(views.cmd_go(s, ref))
        case "rewind" if len(args) <= 1:
            if args:
                ref = args[0]
            elif not s.log.state.nodes:
                typer.echo(tr("还没有可回退的节点"))
                return True
            elif sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
                from kaipi.tui import pick_rewind

                ref = pick_rewind(s) or ""
            else:
                emit(views.cmd_tree(s))
                ref = typer.prompt(tr("目标节点（回车取消）"), default="", show_default=False)
            if not ref:
                return True
            typer.echo(views.rewind_preview(s, ref))
            from kaipi.configuration import prompt

            mode = prompt(tr("rewind [both|code|conversation|cancel]"), default="both")
            if mode != "cancel":
                emit(views.cmd_rewind(s, ref, mode))
        case _:
            out = views.verb(s, argv)
            emit(
                out
                if out is not None
                else [tr("{RED}unknown command /{name}{RESET}", RED=RED, name=name, RESET=RESET)]
            )
    return True


# --- typer wiring ---------------------------------------------------------------------


def onboard() -> None:
    """Two things stand between a fresh install and a conversation: somewhere to send the
    request, and something to send it to. Ask for each in turn, and say when it is done."""
    from kaipi.providers import BUILTIN, active_model, auth, configured_models

    has_key = bool(auth().get("providers")) or any(
        cfg.api_key_env and os.environ.get(cfg.api_key_env) for cfg in BUILTIN.values()
    )
    configured = False
    if settings.read().get("language") not in ("zh", "en"):
        if has_key:
            settings.save({"language": "zh"})
        else:
            from kaipi.configuration import choose

            typer.echo("👋 欢迎使用 kaipi / Welcome to kaipi")
            try:
                pick = choose(
                    list(i18n.LANGUAGES.values()),
                    title="选择界面语言 / Choose your language",
                    hint="↑↓ 选择 / Select · Enter 确认 / Confirm · Esc 退出 / Exit",
                )
                settings.save({"language": list(i18n.LANGUAGES)[pick]})
            except (EOFError, KeyboardInterrupt, typer.Abort):
                raise typer.Exit() from None
    if not has_key:
        typer.echo(
            tr(
                "{DIM}第 1 步 / 共 2 步：选一个 API 提供商，填上地址和 key。{RESET}\n",
                DIM=DIM,
                RESET=RESET,
            )
        )
        if not cmd_provider():
            raise typer.Exit()
        configured = True
        typer.echo("")
    if not (os.environ.get("KAIPI_MODEL") or active_model()):
        if configured_models():
            typer.echo(
                tr("{DIM}第 2 步 / 共 2 步：挑一个模型启用。{RESET}\n", DIM=DIM, RESET=RESET)
            )
            if not cmd_model():
                raise typer.Exit()
            configured = True
        else:
            typer.echo(
                tr(
                    "{RED}还没有可用的模型：再跑一次 /provider，把 model id 填上{RESET}",
                    RED=RED,
                    RESET=RESET,
                )
            )
            raise typer.Exit(1)
        typer.echo(
            tr(
                "\n{GREEN}配置完成！直接描述你要做的事，它会自己查看代码、执行命令。{RESET}",
                GREEN=GREEN,
                RESET=RESET,
            )
        )
        typer.echo(
            tr(
                "{DIM}随时 /tree 看分支，Esc 停掉跑歪的一轮，/ledger 看省了多少。{RESET}\n",
                DIM=DIM,
                RESET=RESET,
            )
        )

    if configured:
        clear_terminal()


def _session(*, mutate: bool = True) -> core.Session:
    """The most recent session, for the sub-commands that act on one from outside."""
    if mutate:
        refuse_if_open(Path.cwd())
    elif not list_sessions(Path.cwd()):
        # a read-only command must not conjure an empty session into `kaipi sessions`
        typer.echo(
            tr(
                "{DIM}no session in this directory yet; run `kaipi` to start one{RESET}",
                DIM=DIM,
                RESET=RESET,
            )
        )
        raise typer.Exit(0)
    return core.Session(Path.cwd())


ContinueOpt = Annotated[bool, typer.Option("--continue", "-c", help="接上最近的那个对话")]
ResumeOpt = Annotated[str | None, typer.Option("--resume", "-r", help="接上指定 id 的对话")]


def _open(cont: bool, resume: str | None) -> core.Session:
    """Every `kaipi` is a fresh conversation unless told which old one to pick up."""
    refuse_if_open(Path.cwd())
    if resume is not None:
        return core.Session(Path.cwd(), resume=resume)
    return core.Session(Path.cwd(), new=not cont)


@app.callback()
def main(ctx: typer.Context, cont: ContinueOpt = False, resume: ResumeOpt = None) -> None:
    """kaipi: a fresh conversation in the current directory; -c or -r picks up an old one."""
    if ctx.invoked_subcommand is None:
        clear_terminal()
        onboard()
        run_surfaces(_open(cont, resume), "cli")


@app.command(cls=LocalizedCommand)
def version() -> None:
    """show the installed kaipi version."""
    typer.echo(f"kaipi {update.current()}")


@app.command("update", cls=LocalizedCommand)
def update_command() -> None:
    """check GitHub Releases and update a uv tool installation."""
    refuse_if_open(Path.cwd())
    try:
        plan = update.prepare(typer.echo)
        if plan:
            update.install(plan)
    except (OSError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@app.command(cls=LocalizedCommand)
def canvas(cont: ContinueOpt = False, resume: ResumeOpt = None) -> None:
    """open the conversation on the canvas (browser) instead of the terminal."""
    clear_terminal()
    onboard()
    run_surfaces(_open(cont, resume), "canvas")


@app.command(cls=LocalizedCommand)
def tree() -> None:
    say(views.cmd_tree(_session(mutate=False)))


@app.command(cls=LocalizedCommand)
def raw() -> None:
    """print the exact context the next turn would send, grafts included."""
    say(views.cmd_raw(_session(mutate=False)))


@app.command(cls=LocalizedCommand)
def go(node_id: str) -> None:
    say(views.cmd_go(_session(), node_id))


@app.command(cls=LocalizedCommand)
def graft(
    node_id: str,
    depth: Annotated[str, typer.Option()] = "leaf+summary",
    with_tool: Annotated[list[str] | None, typer.Option("--with-tool")] = None,
) -> None:
    s = _session()
    if depth not in ("leaf", "leaf+summary", "branch"):
        raise typer.BadParameter(tr("depth must be leaf | leaf+summary | branch"))
    say(views.cmd_graft(s, node_id, depth, with_tool or []))  # type: ignore[arg-type]


@app.command(cls=LocalizedCommand)
def archive(node_id: str) -> None:
    say(views.cmd_archive(_session(), node_id))


@app.command(cls=LocalizedCommand)
def restore(node_id: str) -> None:
    say(views.cmd_restore(_session(), node_id))


@app.command(cls=LocalizedCommand)
def rewind(
    node_id: str,
    mode: Annotated[str, typer.Option(help="both | code | conversation")] = "both",
) -> None:
    """rewind code and/or conversation to a node; only agent-touched files are restored."""
    say(views.cmd_rewind(_session(), node_id, mode))


trunk_app = typer.Typer(cls=LocalizedGroup, invoke_without_command=True)
app.add_typer(trunk_app, name="trunk")


@trunk_app.callback()
def trunk(ctx: typer.Context) -> None:
    """show the trunk; `kaipi trunk pin <id>` re-pins it."""
    if ctx.invoked_subcommand is None:
        say(views.cmd_trunk(_session(mutate=False), None))


@trunk_app.command("pin", cls=LocalizedCommand)
def trunk_pin(node_id: str) -> None:
    say(views.cmd_trunk(_session(), node_id))


@app.command("ledger", cls=LocalizedCommand)
def ledger_cmd() -> None:
    say(views.cmd_ledger(_session(mutate=False)))


@app.command("status", cls=LocalizedCommand)
def status_cmd() -> None:
    """show the current model, session, context usage and estimated cost."""
    say(views.cmd_status(_session(mutate=False)))


@app.command(cls=LocalizedCommand)
def sessions() -> None:
    say([line for _, line in views.session_rows(Path.cwd())])


@app.command(cls=LocalizedCommand)
def provider() -> None:
    """configure an API provider: endpoint, key, and the model ids it offers you."""
    cmd_provider()


@app.command(cls=LocalizedCommand)
def model() -> None:
    """switch models across every provider you configured."""
    cmd_model()


@app.command(cls=LocalizedCommand)
def config() -> None:
    """open settings; use arrow keys to change preferences immediately."""
    from kaipi import configuration

    configuration.config()


@app.command(cls=LocalizedCommand)
def statusline() -> None:
    """customize status-line elements; Esc saves and returns."""
    from kaipi import configuration

    configuration.statusline()


if __name__ == "__main__":
    sys.exit(app())
