"""CLI (§8): sub-commands and the same verbs as slash commands inside the interactive loop."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from prompt_toolkit.formatted_text import ANSI, fragment_list_width, to_formatted_text
from pydantic import BaseModel, Field
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from kaipi import (
    agent,
    context,
    graph,
    guard,
    history,
    i18n,
    ledger,
    providers,
    settings,
    summarize,
    titles,
    update,
)
from kaipi.display import columns
from kaipi.i18n import tr
from kaipi.model import (
    Depth,
    NodeArchived,
    NodeRestored,
    ReferenceEdge,
    SessionRenamed,
    SessionStarted,
    TrunkPinned,
    blocks,
    new_id,
    text_of,
)
from kaipi.providers import Provider
from kaipi.store import Log, State, kaipi_dir, list_sessions, sessions_dir


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
    """Translate help at render time, without changing Click's global strings."""

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
BASE_PROMPT = """You are kaipi, a coding agent working inside the user's repository.
Your only tool is `bash`; use it to inspect, edit (via shell tools) and test code.
Be terse. Stop and reply with a short final answer when the task is done or you are blocked.
Never run destructive git commands unless the user asked for them."""


def short(node_id: str) -> str:
    return node_id[-6:]


def _home(path: Path, limit: int = 34) -> str:
    """The working directory for the status bar. Shortened under $HOME, and elided from the
    left past `limit`: the numbers to its right are the point of the bar, and a deep path -
    a macOS $TMPDIR is 60 characters before it says anything - would push them off screen."""
    try:
        out = "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        out = str(path)
    if len(out) <= limit:
        return out
    parts = out.split("/")
    for keep in range(2, len(parts)):  # widen from the tail until it no longer fits
        tail = "…/" + "/".join(parts[-keep:])
        if len(tail) > limit:
            return "…/" + "/".join(parts[-(keep - 1) :])
    return out


def delta_line(before: int, after: int) -> str:
    sign = "+" if after >= before else "-"
    return tr(
        "主干上下文：{before} → {after}（{change}）",
        before=ledger.fmt(before),
        after=ledger.fmt(after),
        change=sign + ledger.fmt(abs(after - before)),
    )


def clear_terminal() -> None:
    if sys.stdout.isatty():
        typer.echo("\033[2J\033[3J\033[H", nl=False)


def say(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


# --- surface lock: a session is open in exactly one place (CLI or canvas) at a time ----


def _lock_path(cwd: Path) -> Path:
    return cwd / ".kaipi" / "lock.json"


def lock_holder(cwd: Path) -> dict[str, object] | None:
    """The live process holding this directory's session, if it is not us."""
    p = _lock_path(cwd)
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text())
        pid = int(info["pid"])
        if pid == os.getpid():
            return None
        return dict(info) if _alive(pid) else None
    except (ValueError, KeyError, TypeError):
        return None


def _alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) would terminate the process there
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)  # type: ignore[attr-defined]
        if h:
            ctypes.windll.kernel32.CloseHandle(h)  # type: ignore[attr-defined]
        return bool(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # stale lock from a process that died
    except PermissionError:
        pass  # alive but not ours
    return True


def set_lock(cwd: Path, surface: str, url: str = "") -> None:
    kaipi_dir(cwd)
    p = _lock_path(cwd)
    p.write_text(json.dumps({"pid": os.getpid(), "surface": surface, "url": url}))


def release_lock(cwd: Path) -> None:
    _lock_path(cwd).unlink(missing_ok=True)


def refuse_if_open(cwd: Path) -> None:
    h = lock_holder(cwd)
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


class Cursor(BaseModel):
    """`.kaipi/cursor.json`: where the user is standing. kaipi is its only writer."""

    session: str = ""
    leaf: str | None = None
    pending: list[ReferenceEdge] = Field(default_factory=list)


class Session:
    """Everything a command needs: the log, the cursor (current leaf + pending grafts),
    pricing and lazily built providers."""

    def __init__(self, cwd: Path, *, new: bool = False, resume: str | None = None) -> None:
        """`new`: a fresh conversation (empty leftovers from earlier fresh starts are
        dropped rather than piled up). `resume`: a session id, or a prefix/suffix of one.
        Neither: the most recent session, which is what the sub-commands act on."""
        self.cwd = cwd
        self.ansi = True  # the terminal colours what the verbs say; the canvas turns this off
        self.pricing = ledger.Pricing.load()
        self.cursor_path = cwd / ".kaipi" / "cursor.json"
        existing = list_sessions(cwd)
        if new:
            for p in existing:
                if not Log(p).state.nodes:
                    p.unlink()
            self.log = self._start()
        elif resume is not None:
            self.log = Log(find_session(cwd, resume))
        elif not existing:
            self.log = self._start()
        else:
            cur = self._cursor()
            path = sessions_dir(cwd) / (cur.session or existing[-1].name)
            self.log = Log(path if path.exists() else existing[-1])
        self._place()
        self._provider: Provider | None = None
        self._provider_spec: str | None = None  # what it was built for; None if injected
        self._cheap: Provider | None = None
        self._cheap_for: str | None = None  # the spec it was built for; None if injected

    def _cursor(self) -> Cursor:
        if self.cursor_path.exists():
            return Cursor.model_validate_json(self.cursor_path.read_text())
        return Cursor()

    def _place(self) -> None:
        """Leaf and staged grafts for the open log: the cursor's if it points into this
        session, otherwise the trunk leaf."""
        cur = self._cursor()
        mine = cur.session == self.log.path.name
        self.leaf = cur.leaf if mine and cur.leaf in self.log.state.nodes else None
        self.pending = cur.pending if mine else []
        if self.leaf is None:
            self.leaf = graph.trunk(self.log.state)

    def switch(self, path: Path) -> None:
        """`/resume` inside a session: open another log in place."""
        self.save()
        self.log = Log(path)
        self._place()
        self.save()

    def wanted_model(self) -> str:
        """What a session started right now would use: the environment, then `/model`'s
        choice, then the packaged default."""
        from kaipi.providers import active_model

        return os.environ.get("KAIPI_MODEL") or active_model() or self.pricing.model

    def _start(self) -> Log:
        model = self.wanted_model()
        system = BASE_PROMPT
        agents_md = self.cwd / "AGENTS.md"
        if agents_md.exists():  # snapshotted: the system prompt is frozen for the session
            system += "\n\n# Project instructions (AGENTS.md)\n" + agents_md.read_text()
        log = Log(sessions_dir(self.cwd) / f"{new_id()}.jsonl")
        log.append(
            SessionStarted(
                session_id=log.path.stem, cwd=str(self.cwd), model=model, system_prompt=system
            )
        )
        return log

    def save(self) -> None:
        kaipi_dir(self.cwd)
        cur = Cursor(session=self.log.path.name, leaf=self.leaf, pending=self.pending)
        self.cursor_path.write_text(cur.model_dump_json())

    def _build(self, model: str) -> Provider:
        from kaipi.providers import ProviderConfig, build

        price = self.pricing.find(model)
        if price is None:
            typer.echo(
                tr(
                    "{RED}warning: {model} not in pricing.toml; costs will read 0{RESET}",
                    RED=RED,
                    model=model,
                    RESET=RESET,
                )
            )
        cfgs = {k: ProviderConfig(**v) for k, v in self.pricing.providers.items()}
        try:
            # A spec that names its provider is routed there; only a bare id borrows the
            # provider its price row names.
            return build(
                model,
                cfgs,
                provider_of=None if "/" in model else (price.provider if price else None),
                adaptive_thinking=price.adaptive_thinking if price else True,
            )
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e

    @property
    def provider(self) -> Provider:
        """The model the next turn uses: whatever `/model` (or the environment) says right
        now, not what the session started with. Each node records the model it ran on, so
        switching mid-session is ordinary; it costs one cache write, and on Anthropic the
        thinking blocks a different model produced are dropped from the prefix."""
        spec = self.wanted_model()
        if self._provider is None or self._provider_spec not in (None, spec):
            self._provider, self._provider_spec = self._build(spec), spec
        return self._provider

    def vendor(self, spec: str) -> str:
        """Who a model spec would be routed to, or "" when nothing says. Deliberately not
        `pricing.split`, whose fallback answers "anthropic" for an unlisted bare id - a
        guess is exactly what must not decide where a conversation is sent."""
        if "/" in spec:
            return spec.partition("/")[0]
        price = self.pricing.find(spec)
        return price.provider if price else ""

    @property
    def cheap(self) -> Provider:
        """Graft summaries stay with the provider the session is on. `pricing.toml` names one
        cheap model for everyone and it belongs to some vendor - taking it would send the
        user's conversation to an account they never chose, or fail for want of a key they
        never had. It is used only when both specs name the same vendor, and it is rebuilt
        on a model switch for the same reason `provider` is."""
        spec = self.wanted_model()
        if self._cheap is None or self._cheap_for not in (None, spec):
            summary = self.pricing.summary_model
            mine = self.vendor(spec)
            try:
                usable = bool(mine) and self.vendor(summary) == mine
                self._cheap = self._build(summary) if usable else self.provider
            except (typer.BadParameter, ValueError):
                self._cheap = self.provider
            self._cheap_for = spec
        return self._cheap

    def resolve(self, ref: str) -> str:
        found = self.log.state.find(ref)
        if found is None:
            raise typer.BadParameter(tr("{ref}: 没有这个节点（用 /tree 里的编号，如 N3）", ref=ref))
        return found

    def name(self, node_id: str | None) -> str:
        return self.log.state.label(node_id)

    def palette(self) -> tuple[str, str, str, str, str]:
        """GREEN, RED, DIM, BOLD, RESET for whichever surface holds the session."""
        return (GREEN, RED, DIM, BOLD, RESET) if self.ansi else ("", "", "", "", "")

    def color(self, c: ledger.Color, text: str) -> str:
        green, red, _, _, reset = self.palette()
        return f"{green if c == 'green' else red}{text}{reset}"

    def burden_line(self) -> str:
        return tr("trunk burden: {v0}", v0=ledger.fmt(ledger.trunk_burden(self.log.state)))

    def status(self, width: int = 120) -> str:
        """The bar both surfaces show: where you are, what you are using, what it has cost
        and what the shape of the session has saved. Everything but the path is a number
        worth reading, so the path is what yields when the terminal is narrow."""
        fields = settings.statusline()
        if all(value == "off" for value in fields.values()):
            return ""
        st, spec = self.log.state, self.wanted_model()
        price = self.pricing.price(spec)
        burden = ledger.trunk_burden(st)
        window = (
            f"{ledger.fmt(burden)}/{price.context // 1000}k"
            if price.context
            else ledger.fmt(burden)
        )
        if fields["context"] == "percent":
            window = f"{burden / price.context:.1%}" if price.context else tr("未知")
        saved = ledger.saved_by_kind(st, self.pricing)
        tokens = sum(t for t, _ in saved.values())
        money = sum(c for _, c in saved.values())
        who, model = self.pricing.split(spec)
        spent = "  ".join(
            text
            for key, text in (
                ("context", tr("context {window}", window=window)),
                ("spent", tr("spent ${v0:.4f}", v0=ledger.total_cost(st, self.pricing))),
            )
            if fields[key] != "off"
        )
        figures = []
        if fields["saved_tokens"] != "off":
            figures.append(f"{ledger.fmt(tokens)}/{tr('每轮')}")
        if fields["saved_cost"] != "off":
            figures.append(f"${money:.4f}")
        long = tr("saved ") + " = ".join(figures) if figures else ""
        short_ = tr("saved ${money:.4f}", money=money) if fields["saved_cost"] != "off" else long

        def bar(where: str, figure: str) -> str:
            head = "/".join(
                text for key, text in (("provider", who), ("model", model)) if fields[key] != "off"
            )
            return "  ".join(
                part
                for part in (
                    head,
                    f"{DIM}{where}{RESET}" if where else "",
                    spent,
                    f"{GREEN}{figure}{RESET}" if figure else "",
                )
                if part
            )

        def visible(text: str) -> int:
            return fragment_list_width(to_formatted_text(ANSI(text)))

        # Narrower than everything fits: give up the path first, then the per-turn figure.
        # A number that is not on screen is worth less than a path that is not.
        name = st.name if fields["session"] != "off" else ""
        available = width - visible(bar(name, long)) - (3 if name else 2)
        directory = ""
        if fields["directory"] == "full":
            directory = _home(self.cwd, max(12, available))
        elif fields["directory"] == "compact":
            directory = self.cwd.name or self.cwd.anchor
        for where, figure in (
            (" · ".join(part for part in (name, directory) if part), long),
            (name, long),
            ("", long),
            ("", short_),
        ):
            out = bar(where, figure)
            if visible(out) <= width:
                return out
        return bar("", short_)


# --- verbs (shared by sub-commands, slash commands and the canvas) --------------------
# A verb returns what it has to say; the surface holding the session shows it. The ones
# that have to ask something (a picker, a y/n) live with the terminal, not here.


def tree_rows(
    s: Session, *, compact: bool = False, live_only: bool = False
) -> list[tuple[str, str]]:
    """Tree order and connectors shared by static output and interactive navigation."""
    st = s.log.state
    _, RED, DIM, _, RESET = s.palette()
    trunk = graph.trunk(st)
    trunk_ids = {n.id for n in graph.lineage(st, trunk)} if trunk else set()
    out: list[tuple[str, str]] = []

    def walk(parent: str | None, prefix: str) -> None:
        children = graph.children(st, parent, live_only=live_only)
        for i, n in enumerate(children):
            last = i == len(children) - 1
            branch = "└─ " if last else "├─ "
            status = {
                "archived": tr(" [已归档]"),
                "tombstone": tr(" [未完成]"),
                "aborted": tr(" {RED}[已废弃 · 不进上下文]{RESET}", RED=RED, RESET=RESET),
            }.get(n.status, "")
            tags = (tr(" · 当前") if n.id == s.leaf else "") + (
                tr(" · 主线") if n.id in trunk_ids else ""
            )
            label = " ".join(context.user_input(n).split())[:64]
            line = f"{prefix}{branch}{s.name(n.id)}{tags}  {label}{status}"
            if not compact:
                own = ledger.fmt(ledger.own_tokens(st, n.id))
                cost = s.pricing.price(n.model).cost(n.usage)
                graft = f"  ⟵{','.join(s.name(g.src_id) for g in n.grafts)}" if n.grafts else ""
                line += f" {DIM}{own} tokens ${cost:.4f}{graft}{RESET}"
            out.append((n.id, line if n.status == "live" else f"{DIM}{line}{RESET}"))
            walk(n.id, prefix + ("   " if last else "│  "))

    walk(None, "")
    return out


def cmd_tree(s: Session) -> list[str]:
    _, _, DIM, _, RESET = s.palette()
    out = [line for _, line in tree_rows(s)]
    if s.pending:  # staged but not used yet: no node carries them until the next input
        staged = "  ".join(f"{s.name(e.src_id)}({e.depth})" for e in s.pending)
        out.append(
            tr(
                "{DIM}已登记待嫁接（下一句生效）：{staged}{RESET}",
                DIM=DIM,
                staged=staged,
                RESET=RESET,
            )
        )
    out.append(
        tr("{DIM}主线 · 当前  ⟵嫁接  |  {v1}{RESET}", DIM=DIM, v1=s.burden_line(), RESET=RESET)
    )
    return out


PLACEHOLDER = "「您即将输入的下一句」"


def cmd_raw(s: Session) -> list[str]:
    """Exactly what the next turn would send, assembled by the same `context.build` the agent
    uses (§4), with the input you have not typed yet standing in for itself. It sends
    nothing: the size is `ledger.estimate_tokens`, not the provider's counter, which is an
    HTTP round trip on two of the four protocols. So a `leaf+summary` graft whose summary
    does not exist yet shows `(no summary available)` here and gets one when you really
    send."""
    _, RED, DIM, _, RESET = s.palette()
    st = s.log.state
    ctx = context.build(st, s.leaf, s.pending, PLACEHOLDER, guard=graph.is_exploration(st, s.leaf))
    out = [f"{DIM}=== system prompt ==={RESET}", ctx.system]
    out.append(
        tr(
            "{DIM}--- ↑ 缓存断点（system prompt 末尾，Anthropic 系协议固定放这一个）---{RESET}",
            DIM=DIM,
            RESET=RESET,
        )
    )
    total = ledger.estimate_tokens(ctx.system)
    for i, m in enumerate(ctx.messages):
        out.append(
            tr(
                "{DIM}=== 第 {v1}/{v2} 条消息 · {v3} ==={RESET}",
                DIM=DIM,
                v1=i + 1,
                v2=len(ctx.messages),
                v3=m["role"],
                RESET=RESET,
            )
        )
        for b in blocks(m["content"]):
            kind = str(b.get("type", ""))
            if kind == "tool_use":
                body = f"$ {b.get('input', {}).get('command', '')}"
            elif kind == "tool_result":
                body = text_of(b.get("content", ""))
            else:
                body = str(b.get("text", "")) or f"({kind})"
            out.append(f"{DIM}  [{kind}]{RESET}")
            out.append(body)
            total += ledger.estimate_tokens(body)  # every block, not just the text ones:
        if i in ctx.cache_points:  # a test log is most of what a real turn weighs
            out.append(
                tr(
                    "{DIM}--- ↑ 缓存断点（第 {v1} 条消息末尾）---{RESET}",
                    DIM=DIM,
                    v1=i + 1,
                    RESET=RESET,
                )
            )
    points = len(ctx.cache_points) + 1  # the fixed one on the system prompt is not in the set
    out.append(
        tr(
            "{DIM}=== {v1} 条消息  约 {v2} token（本地估算，没有发任何请求）"
            "  缓存断点 {points} 处 ==={RESET}",
            DIM=DIM,
            v1=len(ctx.messages),
            v2=ledger.fmt(total),
            points=points,
            RESET=RESET,
        )
    )
    stale = [
        g.src_id for g in ctx.grafts if g.depth == "leaf+summary" and not st.nodes[g.src_id].summary
    ]
    if stale:
        names = "、".join(s.name(i) for i in stale)
        out.append(
            tr(
                "{RED}{names} 的摘要还没生成，真正发送时才会生成{RESET}",
                RED=RED,
                names=names,
                RESET=RESET,
            )
        )
    return out


def cmd_go(s: Session, ref: str) -> list[str]:
    nid = s.resolve(ref)
    if s.log.state.nodes[nid].status != "live":
        raise typer.BadParameter(tr("{v0} is not live; restore it first", v0=s.name(nid)))
    st = s.log.state
    t = graph.trunk(st)
    if t is not None and nid != t and st.trunk_pin != t:
        # Leaving the trunk leaf to branch: pin it, or an equally deep sibling would steal it.
        s.log.append(TrunkPinned(node_id=t))
    s.leaf = nid
    kind = tr("ui.trunk" if nid == t else "ui.exploration")
    return [tr("at {v0} ({kind}); next input opens a branch here", v0=s.name(nid), kind=kind)]


def cmd_graft(s: Session, ref: str, depth: Depth, with_tool: list[str]) -> list[str]:
    src = s.resolve(ref)
    edge = ReferenceEdge(src_id=src, dst_id="", depth=depth, include_tool_outputs=with_tool)
    st = s.log.state
    preview = ledger.graft_preview(st, s.leaf, edge, s.provider.count_tokens)
    s.pending.append(edge)
    before = ledger.trunk_burden(st)
    on_trunk = s.leaf == graph.trunk(st)
    out = [
        tr("graft {v0} -> next node  depth={depth}  ", v0=s.name(src), depth=depth)
        + "  ".join(f"{d}: +{ledger.fmt(n)}" for d, n in preview.items())
    ]
    after = before + preview[depth] if on_trunk else before
    out.append(
        s.color("green", delta_line(before, after)) + "  (" + tr("仅追加，缓存前缀不变") + ")"
    )
    if with_tool:
        out.append(tr("including tool outputs: {v0}", v0=", ".join(with_tool)))
    return out


def cmd_archive(s: Session, ref: str) -> list[str]:
    _, _, DIM, _, RESET = s.palette()
    out = _set_status(s, ref, NodeArchived, "archived")
    return [
        *out,
        tr(
            "{DIM}restore with /restore {v1}{RESET}",
            DIM=DIM,
            v1=s.name(s.resolve(ref)),
            RESET=RESET,
        ),
    ]


def cmd_restore(s: Session, ref: str) -> list[str]:
    return _set_status(s, ref, NodeRestored, "restored")


def _set_status(
    s: Session, ref: str, event: type[NodeArchived] | type[NodeRestored], word: str
) -> list[str]:
    """Archive or restore a whole subtree (one atomic operation, §2.1) and price it."""
    nid = s.resolve(ref)
    ids = graph.subtree(s.log.state, nid)
    before = ledger.trunk_burden(s.log.state)
    s.log.append(event(ids=ids))
    if s.leaf not in s.log.state.nodes or s.log.state.nodes[s.leaf].status != "live":
        s.leaf = graph.trunk(s.log.state)  # the cursor left with the subtree
    c, _ = ledger.cache_color(s.log.state)
    why = tr("缓存前缀改变，需重写缓存并丢弃思考块" if c == "red" else "仅追加，缓存前缀不变")
    return [
        tr("{word} {v1} node(s) under {v2}", word=tr("ui." + word), v1=len(ids), v2=s.name(nid)),
        s.color(c, delta_line(before, ledger.trunk_burden(s.log.state))) + f"  ({why})",
    ]


def cmd_trunk(s: Session, ref: str | None) -> list[str]:
    st = s.log.state
    if ref is None:
        pin = st.trunk_pin
        heur = graph.trunk(st.model_copy(update={"trunk_pin": None}))
        return [
            tr(
                "主干：{node}  固定：{pin}  自动选择：{heuristic}",
                node=s.name(graph.trunk(st)),
                pin=s.name(pin) if pin else tr("ui.no"),
                heuristic=s.name(heur) if heur else "-",
            )
        ]

    nid = s.resolve(ref)
    before = ledger.trunk_burden(st)
    s.log.append(TrunkPinned(node_id=nid))
    out = [tr("trunk pinned to {v0}", v0=s.name(nid))]
    if s.leaf != nid and graph.trunk(s.log.state) == nid:
        # Pinning a branch means building on it; left behind, the next turn would be a
        # read-only exploration. Users tripped on that twice.
        s.leaf = nid
        out.append(tr("cursor moved to {v0}", v0=s.name(nid)))
    delta = delta_line(before, ledger.trunk_burden(s.log.state))
    return [*out, s.color("green", delta) + tr("  (no prefix changes)")]


def rewind_paths(state: State, node_id: str) -> list[str]:
    """Files touched by every turn after node_id: the only files a code rewind restores."""
    seq = state.nodes[node_id].seq
    return sorted({p for n in state.nodes.values() if n.seq > seq for p in n.paths})


def rewind_code(state: State, cwd: Path, node_id: str) -> list[str]:
    """Restore the agent-touched files to their state when node_id ended (Claude-Code-style:
    files the agent never touched are left alone). The pre-rewind tree is pinned at
    refs/kaipi/undo so the step itself can be undone with git."""
    n = state.nodes[node_id]
    if not n.tree:
        raise typer.BadParameter(
            tr("{v0} has no snapshot (not a git repo?)", v0=state.label(node_id))
        )
    paths = rewind_paths(state, node_id)
    cur = guard.snapshot(cwd)
    if cur:
        guard.keep(cwd, "undo", cur)
    guard.restore(cwd, n.tree, paths)
    return paths


def rewind_preview(s: Session, ref: str) -> str:
    nid = s.resolve(ref)
    paths = rewind_paths(s.log.state, nid)
    return tr(
        "files a code rewind to {v0} would restore: {v1}",
        v0=s.name(nid),
        v1=", ".join(paths) or tr("无"),
    )


def cmd_rewind(s: Session, ref: str, mode: str) -> list[str]:
    nid = s.resolve(ref)
    if mode not in ("both", "code", "conversation"):
        raise typer.BadParameter(tr("mode must be both | code | conversation"))
    if mode != "code" and s.log.state.nodes[nid].status != "live":
        raise typer.BadParameter(
            tr("{v0} is not live; restore it or rewind code only", v0=s.name(nid))
        )
    out = []
    if mode in ("both", "code"):
        done = rewind_code(s.log.state, s.cwd, nid)
        out.append(
            tr(
                "restored {v0} file(s) to {v1}; previous state at refs/kaipi/undo",
                v0=len(done),
                v1=s.name(nid),
            )
        )
    if mode in ("both", "conversation"):
        out += cmd_go(s, nid)
    return out


def cmd_provider(*, report: Callable[[str], None] = typer.echo) -> bool:
    from kaipi import configuration

    return configuration.run(lambda: configuration.provider(report=report))


def cmd_model(*, report: Callable[[str], None] = typer.echo) -> bool:
    from kaipi import configuration

    return configuration.run(lambda: configuration.model(report=report))


def cmd_status(s: Session) -> list[str]:
    """A local snapshot of this session; never build a provider or make an API call."""
    from importlib.metadata import version

    st, spec = s.log.state, s.wanted_model()
    r = ledger.report(st, s.pricing)
    price = s.pricing.find(spec)
    node = st.nodes.get(s.leaf or "")
    used = node.context_tokens if node and node.completed else None
    window = f"{price.context:,} tokens" if price and price.context else tr("未知")
    ctx = tr("尚无请求记录") if used is None else f"{used:,} tokens"
    ctx += tr(" / 窗口 {window}", window=window)
    if used is not None and price and price.context:
        ctx += f" · {used / price.context:.1%}"
    models = {n.model for n in st.nodes.values()} | {m for m, _ in st.summaries + st.titles}
    cost_note = (
        tr("部分模型未计价") if any(s.pricing.find(m) is None for m in models) else tr("价格表估算")
    )
    mode = tr("探索（只读）") if graph.is_exploration(st, s.leaf) else tr("主干续写")
    u = r.usage
    rows = [
        (tr("模型"), spec),
        (tr("提供商"), s.vendor(spec) or tr("未指定")),
        (tr("会话"), f"{st.name or tr('未命名')} · {st.session_id}"),
        (tr("目录"), str(s.cwd)),
        (
            tr("位置"),
            tr("{v0} · {mode} · 主干 {v2}", v0=s.name(s.leaf), mode=mode, v2=s.name(r.trunk)),
        ),
        (tr("上下文"), ctx),
        (tr("待嫁接"), tr("{v0} 个节点", v0=len(s.pending))),
        (tr("累计用量"), tr("输入 {v0:,} · 输出 {v1:,} tokens", v0=u.context, v1=u.output)),
        (
            tr("缓存读取"),
            f"{u.cache_read:,} tokens"
            + (tr(" · 命中 {v0:.1%}", v0=u.cache_read / u.context) if u.context else ""),
        ),
        (tr("会话费用"), f"${r.total_cost:.4f} ({cost_note})"),
    ]
    _, _, dim, bold, reset = s.palette()
    return [
        tr("\n{bold}kaipi {v1} · 状态{reset}\n", bold=bold, v1=version("kaipi"), reset=reset),
        *(f"  {row}" for row in columns(rows)),
        tr(
            "\n  {dim}上下文为当前节点最近请求记录；待嫁接内容未计入。{reset}\n",
            dim=dim,
            reset=reset,
        ),
    ]


def cmd_ledger(s: Session) -> list[str]:
    GREEN, RED, DIM, BOLD, RESET = s.palette()
    r = ledger.report(s.log.state, s.pricing)
    u = r.usage
    out = [
        tr(
            "{BOLD}session {v1}  started on {v2}{RESET}",
            BOLD=BOLD,
            v1=s.log.path.stem,
            v2=s.log.state.model,
            RESET=RESET,
        ),
        tr(
            "total cost: ${v0:.4f}   tokens: uncached {v1}  cache write {v2}"
            "  cache read {v3}  output {v4}",
            v0=r.total_cost,
            v1=ledger.fmt(u.input_uncached),
            v2=ledger.fmt(u.cache_write),
            v3=ledger.fmt(u.cache_read),
            v4=ledger.fmt(u.output),
        ),
        tr(
            "trunk: {v0}   trunk burden: {v1}",
            v0=s.name(r.trunk) if r.trunk else "-",
            v1=ledger.fmt(r.trunk_burden),
        ),
    ]
    if r.branches:
        out.append(tr("explorations (one-time cost | tokens kept out of the trunk):"))
        out.extend(
            f"  {row}"
            for row in columns(
                [
                    (
                        s.name(b.leaf_id),
                        tr("ui." + b.status),
                        tr("{v0} 个节点", v0=b.nodes),
                        f"${b.one_time_cost:.4f}",
                        ledger.fmt(b.blocked_tokens),
                    )
                    for b in r.branches
                ]
            )
        )
    saved = ledger.saved_by_kind(s.log.state, s.pricing)
    labels = {
        "exploration": tr("探索分支未并入主干"),
        "graft": tr("嫁接带的是结论而非整条分支"),
        "interrupt": tr("打断的回合不进上下文"),
    }
    total_tok = sum(t for t, _ in saved.values())
    out.append(
        tr(
            "{GREEN}saved {v1} per trunk turn = ${v2:.4f} not spent so far{RESET}",
            GREEN=GREEN,
            v1=ledger.fmt(total_tok),
            v2=sum((c for _, c in saved.values())),
            RESET=RESET,
        )
    )
    for kind, (tok, money) in saved.items():
        if tok:
            out.append(
                tr(
                    "  {v0:>7}/turn  ${money:<8.4f} {DIM}{v3}{RESET}",
                    v0=ledger.fmt(tok),
                    money=money,
                    DIM=DIM,
                    v3=labels[kind],
                    RESET=RESET,
                )
            )
    dropped = sum(n.dropped_thinking for n in s.log.state.nodes.values())
    if dropped:
        out.append(
            tr(
                "{RED}thinking blocks dropped by the API (prefix edits): {dropped}{RESET}",
                RED=RED,
                dropped=dropped,
                RESET=RESET,
            )
        )
    return out


def find_session(cwd: Path, ref: str) -> Path:
    hits = [
        p
        for p in list_sessions(cwd)
        if p.stem == ref or p.stem.endswith(ref) or p.stem.startswith(ref)
    ]
    if len(hits) != 1:
        raise typer.BadParameter(
            f"{ref}: {tr('没有这个会话') if not hits else tr('匹配到多个会话，请输入更长的 ID')}"
        )
    return hits[0]


def session_title(st: State) -> str:
    """What a session is about: its name, else the question that opened it."""
    if st.name:
        return st.name
    first = next((context.user_input(n) for n in st.nodes.values() if n.parent_id is None), "")
    return first.splitlines()[0][:40] if first else tr("(空对话)")


def session_rows(cwd: Path, current: str = "") -> list[tuple[Path, str]]:
    """(path, one line) per session, newest first."""
    rows = []
    for p in reversed(list_sessions(cwd)):
        st = Log(p).state
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        mark = tr(" (当前)") if p.name == current else ""
        rows.append(
            (
                p,
                tr(
                    "{v0}  {when}  {v2:>3} 轮  {v3}{mark}",
                    v0=short(p.stem),
                    when=when,
                    v2=len(st.nodes),
                    v3=session_title(st),
                    mark=mark,
                ),
            )
        )
    return rows


def pick_session(s: Session) -> str | None:
    """Use the keyboard menu on a tty, with a numbered fallback for plain input."""
    if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
        from kaipi.tui import pick_session as picker

        return picker(s)
    rows = session_rows(s.cwd, s.log.path.name)
    typer.echo(tr("{BOLD}这个目录里的对话{RESET}", BOLD=BOLD, RESET=RESET))
    for i, (_, line) in enumerate(rows, 1):
        typer.echo(f"  {i:>2}. {line}")
    pick = typer.prompt(tr("序号（回车取消）"), default=0, type=int, show_default=False)
    return rows[pick - 1][0].stem if 1 <= pick <= len(rows) else None


def cmd_new(s: Session) -> list[str]:
    """Save the old conversation and switch to an independent, freshly snapshotted log."""
    previous = s.log
    s.switch(s._start().path)
    if not previous.state.nodes:
        previous.path.unlink(missing_ok=True)
    return [tr("已开始新对话。输入 /resume 可找回之前的对话。")]


def cmd_resume(s: Session, ref: str) -> list[str]:
    """Pick up an earlier conversation in this directory."""
    GREEN, _, DIM, _, RESET = s.palette()
    path = find_session(s.cwd, ref)
    if path.name == s.log.path.name:
        return [tr("{DIM}已经在这个对话里{RESET}", DIM=DIM, RESET=RESET)]
    s.switch(path)
    st = s.log.state
    return [
        tr(
            "{GREEN}接上 {v1}  {v2}{RESET}  {DIM}{v5} 轮，光标在 {v6}{v7}",
            GREEN=GREEN,
            v1=short(st.session_id),
            v2=session_title(st),
            RESET=RESET,
            DIM=DIM,
            v5=len(st.nodes),
            v6=s.name(s.leaf),
            v7=RESET,
        )
    ]


def cmd_rename(s: Session, name: str) -> list[str]:
    GREEN, _, _, _, RESET = s.palette()
    name = name.strip()
    if not name:
        raise typer.BadParameter(tr("用法：/rename <名字>"))
    s.log.append(SessionRenamed(name=name))
    return [tr("{GREEN}这个对话现在叫「{name}」{RESET}", GREEN=GREEN, name=name, RESET=RESET)]


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
    s: Session,
    text: str,
    hook: agent.Hook = print_hook,
    *,
    explore: bool = False,
    stop: threading.Event | None = None,
) -> tuple[str, bool]:
    """Run one turn under the cursor. Returns (node id, working tree dirtied).
    `explore` forces an exploration even from the trunk leaf (the trunk is pinned in place).
    `stop` lets a surface interrupt the turn; agent.Interrupted then reaches the caller."""
    st = s.log.state
    t = graph.trunk(st)
    exploration = graph.is_exploration(st, s.leaf, force=explore)
    if exploration and s.leaf == t and st.trunk_pin != t:
        s.log.append(TrunkPinned(node_id=t))
    edges = s.pending
    s.pending = []  # cleared before anything can fail: a staged graft must never strand the
    use: list[ReferenceEdge] = []  # session: a summary the cheap model refused used to do that
    for e in edges:
        if e.depth == "leaf+summary":
            try:
                summarize.ensure_summary(s.log, s.cheap, e.src_id, s.leaf)
            except Exception as err:  # noqa: BLE001 - the graft is the point, the summary a nicety
                # The downgrade is for this attempt only: `edges` goes back to the cursor if
                # the turn is stopped, and what the user staged is what they get on the retry.
                e = e.model_copy(update={"depth": "leaf"})
                why = providers.redact(str(err))  # a gateway can echo the key in its error
                hook(
                    "ledger",
                    tr("[{v0} 摘要没生成：{why}] 改按 leaf 嫁接", v0=s.name(e.src_id), why=why),
                )
        use.append(e)
    try:
        nid = agent.run_turn(
            s.log,
            s.provider,
            s.leaf,
            use,  # this attempt's edges; `edges` is what the cursor gets back if it is stopped
            text,
            exploration=exploration,
            cwd=s.cwd,
            hook=hook,
            stop=stop,
        )
    except (agent.Interrupted, agent.Failed):
        # Whether the user stopped it or the provider did, the turn is over: the cursor goes
        # back to the parent so the next input opens a sibling rather than continuing from
        # the wreck, and the grafts they staged are theirs, not the discarded turn's.
        dead = next(n for n in reversed(list(s.log.state.nodes.values())) if n.status == "aborted")
        s.leaf, s.pending = dead.parent_id, edges
        cost = s.pricing.price(dead.model).cost(dead.usage)
        hook(
            "ledger",
            tr("[{v0} 已废弃] ${cost:.4f}  不会进入之后的上下文", v0=s.name(dead.id), cost=cost),
        )
        raise
    if not exploration and st.trunk_pin is not None and st.trunk_pin == s.leaf:
        s.log.append(TrunkPinned(node_id=nid))  # extending the pinned leaf moves the pin
    s.leaf = nid
    n = s.log.state.nodes[nid]
    if (
        n.parent_id is None
        and sum(node.completed for node in s.log.state.nodes.values()) == 1
        and not s.log.state.name
        and not s.log.state.titles
        and not (stop and stop.is_set())
    ):
        try:
            hook("phase", "生成标题")
            s.log.append(titles.generate(str(text), s.cheap))
        except Exception:  # noqa: BLE001 - optional metadata must never fail a completed turn
            pass
    cost = s.pricing.price(n.model).cost(n.usage)
    kind = tr("ui.exploration" if exploration else "ui.trunk")
    hook(
        "ledger",
        tr(
            "[{v0} {kind}] ${cost:.4f}  cache read {v3}/{v4}  {v5}",
            v0=s.name(nid),
            kind=kind,
            cost=cost,
            v3=ledger.fmt(n.usage.cache_read),
            v4=ledger.fmt(n.usage.context),
            v5=s.burden_line(),
        ),
    )
    return nid, n.guard_dirty


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


def after_failure(s: Session, err: agent.Failed) -> None:
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


def after_interrupt(s: Session, stopped: agent.Interrupted) -> None:
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
        guard.reset(s.cwd)
    typer.echo(
        tr(
            "{DIM}下一句会从 {v1} 重新长出一个兄弟节点{RESET}",
            DIM=DIM,
            v1=s.name(s.leaf),
            RESET=RESET,
        )
    )


def after_turn(s: Session, nid: str, dirty: bool) -> None:
    if dirty:
        if typer.confirm(
            tr("revert working tree (git checkout -- . && git clean -fd)?"), default=False
        ):
            guard.reset(s.cwd)
        elif typer.confirm(tr("pin trunk to {v0} instead?", v0=s.name(nid)), default=False):
            say(cmd_trunk(s, nid))


def cli_turn(s: Session, text: str, *, explore: bool = False) -> None:
    try:
        with EscWatcher() as stop:
            nid, dirty = run_input(s, text, explore=explore, stop=stop)
    except agent.Interrupted as stopped:
        after_interrupt(s, stopped)
        return
    except agent.Failed as err:
        after_failure(s, err)
        return
    after_turn(s, nid, dirty)


# (name, arguments, what it does): the one list behind /help, the start-up line and the
# slash completion in the terminal. Order is the order they are shown in.
COMMANDS: list[tuple[str, str, str]] = [
    ("tree", "", "会话树导航：方向键选择节点，回车从此继续"),
    ("raw", "", "原样打印下一轮要发给模型的全部内容，含嫁接块；不发送、不花钱"),
    ("inspect-context", "", "导出下一轮上下文为 Markdown，在 workspace 根目录查看"),
    ("explore", "<你的问题>", "从当前位置岔出去问一句，主干钉在原地不动"),
    ("go", "[节点id]", "跳转到节点；不带参数打开会话树导航"),
    (
        "graft",
        "<节点id> [--depth leaf|leaf+summary|branch] [--with-tool <id>...]",
        "把那个节点嫁接进下一句输入，只搬结论",
    ),
    ("archive", "<节点id>", "把节点连同整棵子树收起来，退出上下文"),
    ("restore", "<节点id>", "撤销归档"),
    ("trunk", "[pin <节点id>]", "显示主干，或把主干钉到某个节点"),
    ("rewind", "[节点id] [both|code|conversation]", "回退：上下键选节点，再选回退方式"),
    ("status", "", "当前状态：模型、会话、上下文、用量与费用"),
    ("version", "", "显示当前 kaipi 版本"),
    ("update", "", "检查并安装新版，完成后接回当前会话"),
    ("ledger", "", "总账：花费、各项 token、探索/嫁接/打断各省了多少"),
    ("canvas", "", "把会话搬到浏览器（画布里敲 /cli 搬回来）"),
    ("provider", "", "配置 API 提供商：地址、key、模型 id"),
    ("model", "", "换一个模型，下一句开始生效"),
    ("config", "", "设置：上下选择、左右修改，立即生效，Esc 返回"),
    ("statusline", "", "定制底部状态行：上下选择、左右切换选项，Esc 保存返回"),
    ("new", "", "结束当前对话、清空显示，进入新对话；旧对话保留"),
    ("resume", "[对话id]", "接上旧对话；不带参数用上下键选择，回车进入"),
    ("rename", "<名字>", "给当前对话起个名字"),
    ("help", "", "显示这份清单"),
    ("quit", "", "结束（/exit 也一样）"),
]
NODE_ARG = {"go", "graft", "archive", "restore", "rewind"}  # first argument is a node id

HELP = "🌱 欢迎使用 kaipi，输入 /help 查看所有指令及用法。"


def cmd_help(s: Session) -> list[str]:
    _, _, DIM, BOLD, RESET = s.palette()
    rows = [
        (f"{BOLD}{f'/{name} {tr(args)}'.strip()}{RESET}", f"{DIM}{tr(what)}{RESET}")
        for name, args, what in COMMANDS
    ]
    out = [f"  {row}" for row in columns(rows)]
    return [
        *out,
        tr("  {DIM}Esc 停止当前回合 · 运行中敲的话会排队 · ↑↓ 翻历史{RESET}", DIM=DIM, RESET=RESET),
    ]


def interactive(s: Session) -> str:
    """The terminal loop. Returns "canvas" when the user hands the session to the canvas.
    On a tty this is the fixed-bottom layout in `kaipi.tui`; anywhere else (a pipe, the
    tests) a plain line loop that prints the status bar once."""
    set_lock(s.cwd, "cli")
    s.ansi = True  # back from the canvas, which turned colours off
    typer.echo(f"{DIM}{tr(HELP)}{RESET}")
    if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
        from kaipi import tui

        return tui.loop(s)
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    typer.echo(f"kaipi  {s.status()}")
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
                    s.save()
                    return "canvas"
                if not slash(s, line[1:].split()):
                    return "quit"
                if s.log.path != previous:
                    history.replay(s, lambda node, text: typer.echo(f"{node}> {text}"), print_hook)
            else:
                cli_turn(s, line)
        except (typer.BadParameter, ValueError) as e:
            typer.echo(f"{RED}{e}{RESET}")
        s.save()


def run_surfaces(s: Session, surface: str) -> None:
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
        release_lock(s.cwd)
        if not s.log.state.nodes and upgrade is None:
            s.log.path.unlink(missing_ok=True)
        if upgrade is None:
            clear_terminal()
            typer.echo(tr("\n  {DIM}已关闭会话{RESET}\n", DIM=DIM, RESET=RESET))
            st = s.log.state
            if st.nodes:
                title = " ".join(session_title(st).split())
                directory = _home(s.cwd, limit=len(str(s.cwd)))
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


def verb(s: Session, argv: list[str]) -> list[str] | None:
    """A slash command either surface can run: what it has to say. None when it is not one
    of those - it asks something, runs a turn, or does not exist."""
    name, args = argv[0], argv[1:]
    if name in NODE_ARG and not args:
        usage = tr(next(a for n, a, _ in COMMANDS if n == name))
        # Interactive commands handle their picker before this shared verb.
        # Other surfaces still need an explicit node.
        usage = usage.replace(tr("[节点id]"), tr("<节点id>"), 1)
        raise typer.BadParameter(tr("用法：/{name} {usage}", name=name, usage=usage))
    match name:
        case "tree":
            return cmd_tree(s)
        case "raw":
            return cmd_raw(s)
        case "inspect-context":
            from kaipi import context_export

            if args:
                raise typer.BadParameter(tr("用法：/inspect-context（不带参数）"))
            try:
                path = context_export.export(s, PLACEHOLDER)
            except OSError as e:
                raise typer.BadParameter(tr("无法导出上下文：{e}", e=e)) from e
            return [tr("上下文已导出。打开此文件查看：\n{path}", path=path)]
        case "go":
            return cmd_go(s, args[0])
        case "graft":
            depth: Depth = "leaf+summary"
            tools: list[str] = []
            if "--depth" in args:
                i = args.index("--depth")
                depth = args[i + 1]  # type: ignore[assignment]
                del args[i : i + 2]
            if "--with-tool" in args:
                i = args.index("--with-tool")
                tools, args = args[i + 1 :], args[:i]
            return cmd_graft(s, args[0], depth, tools)
        case "archive":
            return cmd_archive(s, args[0])
        case "restore":
            return cmd_restore(s, args[0])
        case "trunk":
            return cmd_trunk(s, args[1] if len(args) == 2 and args[0] == "pin" else None)
        case "ledger":
            return cmd_ledger(s)
        case "status":
            return cmd_status(s)
        case "version":
            return [f"kaipi {update.current()}"]
        case "new":
            if args:
                raise typer.BadParameter(tr("用法：/new（不带参数）"))
            return cmd_new(s)
        case "help":
            return cmd_help(s)
        case "resume" if args:
            return cmd_resume(s, args[0])
        case "rename":
            return cmd_rename(s, " ".join(args))
        case "rewind" if len(args) > 1:
            return cmd_rewind(s, args[0], args[1])
    return None


def slash(s: Session, argv: list[str], *, emit: Callable[[list[str]], None] = say) -> bool:
    """A slash command in the terminal. False means quit."""
    if not argv:
        return True
    name, args = argv[0], argv[1:]
    match name:
        case "quit" | "exit":
            return False
        case "new":
            out = verb(s, argv)
            clear_terminal()
            emit(out or [])
        case "provider":
            if cmd_provider(report=lambda text: emit([text])):
                s.pricing = ledger.Pricing.load()
                s._provider = s._cheap = None
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
        case "explore":
            cli_turn(s, " ".join(args), explore=True)
        case "resume" if not args:
            picked = pick_session(s)
            if picked:
                emit(cmd_resume(s, picked))
        case "tree" | "go" if not args:
            if sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("KAIPI_PLAIN"):
                from kaipi.tui import pick_node

                ref = pick_node(s)
                if ref:
                    emit(cmd_go(s, ref))
            else:
                emit(cmd_tree(s))
                if name == "go" and s.log.state.nodes:
                    ref = typer.prompt(tr("目标节点（回车取消）"), default="", show_default=False)
                    if ref:
                        emit(cmd_go(s, ref))
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
                emit(cmd_tree(s))
                ref = typer.prompt(tr("目标节点（回车取消）"), default="", show_default=False)
            if not ref:
                return True
            typer.echo(rewind_preview(s, ref))
            from kaipi.configuration import prompt

            mode = prompt(tr("rewind [both|code|conversation|cancel]"), default="both")
            if mode != "cancel":
                emit(cmd_rewind(s, ref, mode))
        case _:
            out = verb(s, argv)
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


def _session(*, mutate: bool = True) -> Session:
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
    return Session(Path.cwd())


ContinueOpt = Annotated[bool, typer.Option("--continue", "-c", help="接上最近的那个对话")]
ResumeOpt = Annotated[str | None, typer.Option("--resume", "-r", help="接上指定 id 的对话")]


def _open(cont: bool, resume: str | None) -> Session:
    """Every `kaipi` is a fresh conversation unless told which old one to pick up."""
    refuse_if_open(Path.cwd())
    if resume is not None:
        return Session(Path.cwd(), resume=resume)
    return Session(Path.cwd(), new=not cont)


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
    say(cmd_tree(_session(mutate=False)))


@app.command(cls=LocalizedCommand)
def raw() -> None:
    """print the exact context the next turn would send, grafts included."""
    say(cmd_raw(_session(mutate=False)))


@app.command(cls=LocalizedCommand)
def go(node_id: str) -> None:
    s = _session()
    say(cmd_go(s, node_id))
    s.save()


@app.command(cls=LocalizedCommand)
def graft(
    node_id: str,
    depth: Annotated[str, typer.Option()] = "leaf+summary",
    with_tool: Annotated[list[str] | None, typer.Option("--with-tool")] = None,
) -> None:
    s = _session()
    if depth not in ("leaf", "leaf+summary", "branch"):
        raise typer.BadParameter(tr("depth must be leaf | leaf+summary | branch"))
    say(cmd_graft(s, node_id, depth, with_tool or []))  # type: ignore[arg-type]
    s.save()


@app.command(cls=LocalizedCommand)
def archive(node_id: str) -> None:
    s = _session()
    say(cmd_archive(s, node_id))
    s.save()


@app.command(cls=LocalizedCommand)
def restore(node_id: str) -> None:
    s = _session()
    say(cmd_restore(s, node_id))
    s.save()


@app.command(cls=LocalizedCommand)
def rewind(
    node_id: str,
    mode: Annotated[str, typer.Option(help="both | code | conversation")] = "both",
) -> None:
    """rewind code and/or conversation to a node; only agent-touched files are restored."""
    s = _session()
    say(cmd_rewind(s, node_id, mode))
    s.save()


trunk_app = typer.Typer(cls=LocalizedGroup, invoke_without_command=True)
app.add_typer(trunk_app, name="trunk")


@trunk_app.callback()
def trunk(ctx: typer.Context) -> None:
    """show the trunk; `kaipi trunk pin <id>` re-pins it."""
    if ctx.invoked_subcommand is None:
        say(cmd_trunk(_session(mutate=False), None))


@trunk_app.command("pin", cls=LocalizedCommand)
def trunk_pin(node_id: str) -> None:
    s = _session()
    say(cmd_trunk(s, node_id))
    s.save()


@app.command("ledger", cls=LocalizedCommand)
def ledger_cmd() -> None:
    say(cmd_ledger(_session(mutate=False)))


@app.command("status", cls=LocalizedCommand)
def status_cmd() -> None:
    """show the current model, session, context usage and estimated cost."""
    say(cmd_status(_session(mutate=False)))


@app.command(cls=LocalizedCommand)
def sessions() -> None:
    say([line for _, line in session_rows(Path.cwd())])


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
