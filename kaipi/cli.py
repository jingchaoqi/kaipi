"""CLI (§8): sub-commands and the same verbs as slash commands inside the interactive loop."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, Field

from kaipi import agent, context, graph, guard, ledger, summarize
from kaipi.model import (
    Depth,
    NodeArchived,
    NodeRestored,
    ReferenceEdge,
    SessionStarted,
    TrunkPinned,
    new_id,
)
from kaipi.providers import Provider
from kaipi.store import Log, State, kaipi_dir, list_sessions, sessions_dir

app = typer.Typer(add_completion=False, no_args_is_help=False, invoke_without_command=True)
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
BASE_PROMPT = """You are kaipi, a coding agent working inside the user's repository.
Your only tool is `bash`; use it to inspect, edit (via shell tools) and test code.
Be terse. Stop and reply with a short final answer when the task is done or you are blocked.
Never run destructive git commands unless the user asked for them."""


def short(node_id: str) -> str:
    return node_id[-6:]


def _home(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def color(c: ledger.Color, s: str) -> str:
    return f"{GREEN if c == 'green' else RED}{s}{RESET}"


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
        where = f"the canvas at {h['url']}" if h.get("surface") == "canvas" else "another terminal"
        typer.echo(f"{RED}session is open in {where} (pid {h['pid']}); /quit there first{RESET}")
        raise typer.Exit(1)


class Cursor(BaseModel):
    """`.kaipi/cursor.json`: where the user is standing. kaipi is its only writer."""

    session: str = ""
    leaf: str | None = None
    pending: list[ReferenceEdge] = Field(default_factory=list)


class Session:
    """Everything a command needs: the log, the cursor (current leaf + pending grafts),
    pricing and lazily built providers."""

    def __init__(self, cwd: Path, *, new: bool = False) -> None:
        self.cwd = cwd
        self.pricing = ledger.Pricing.load()
        self.cursor_path = cwd / ".kaipi" / "cursor.json"
        cur = Cursor()
        if self.cursor_path.exists() and not new:
            cur = Cursor.model_validate_json(self.cursor_path.read_text())
        existing = list_sessions(cwd)
        if new or not existing:
            self.log = self._start()
            cur = Cursor()
        else:
            path = sessions_dir(cwd) / (cur.session or existing[-1].name)
            self.log = Log(path if path.exists() else existing[-1])
        self.leaf = cur.leaf if cur.leaf in self.log.state.nodes else None
        self.pending = cur.pending
        if self.leaf is None:
            self.leaf = graph.trunk(self.log.state)
        self._provider: Provider | None = None
        self._cheap: Provider | None = None

    def _start(self) -> Log:
        from kaipi.providers import active_model

        model = os.environ.get("KAIPI_MODEL") or active_model() or self.pricing.model
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

        price = self.pricing.models.get(model)
        if price is None:
            typer.echo(f"{RED}warning: {model} not in pricing.toml; costs will read 0{RESET}")
        cfgs = {k: ProviderConfig(**v) for k, v in self.pricing.providers.items()}
        try:
            return build(
                model,
                cfgs,
                provider_of=price.provider if price else None,
                adaptive_thinking=price.adaptive_thinking if price else True,
            )
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            self._provider = self._build(self.log.state.model)
        return self._provider

    @property
    def cheap(self) -> Provider:
        if self._cheap is None:
            self._cheap = self._build(self.pricing.summary_model)
        return self._cheap

    def resolve(self, ref: str) -> str:
        ids = [i for i in self.log.state.nodes if i == ref or i.endswith(ref) or i.startswith(ref)]
        if len(ids) != 1:
            raise typer.BadParameter(f"{ref}: {'no such node' if not ids else 'ambiguous'}")
        return ids[0]

    def burden_line(self) -> str:
        return f"trunk burden: {ledger.fmt(ledger.trunk_burden(self.log.state))}"

    def status(self) -> str:
        """The bar both surfaces show: where you are, what you are using, what it has cost
        and what the shape of the session has saved."""
        st = self.log.state
        price = self.pricing.price(st.model)
        burden = ledger.trunk_burden(st)
        window = (
            f"{ledger.fmt(burden)}/{price.context // 1000}k"
            if price.context
            else ledger.fmt(burden)
        )
        saved = ledger.saved_by_kind(st, self.pricing)
        tokens = sum(t for t, _ in saved.values())
        money = sum(c for _, c in saved.values())
        return (
            f"{price.provider}/{st.model}  {DIM}{_home(self.cwd)}{RESET}  "
            f"context {window}  spent ${ledger.total_cost(st, self.pricing):.4f}  "
            f"{GREEN}saved {ledger.fmt(tokens)}/turn = ${money:.4f}{RESET}"
        )


# --- verbs (shared by sub-commands and slash commands) --------------------------------


def cmd_tree(s: Session) -> None:
    st = s.log.state
    t = graph.trunk(st)
    trunk_ids = {n.id for n in graph.lineage(st, t)} if t else set()

    def walk(parent: str | None, indent: str) -> None:
        for n in graph.children(st, parent, live_only=False):
            mark = "*" if n.id in trunk_ids else " "
            here = ">" if n.id == s.leaf else " "
            status = {
                "archived": " [已归档]",
                "tombstone": " [tombstone]",
                "aborted": f" {RED}[已废弃 · 不进上下文]{RESET}",
            }.get(n.status, "")
            own = ledger.fmt(ledger.own_tokens(st, n.id))
            cost = s.pricing.price(n.model).cost(n.usage)
            label = context.user_input(n)[:44]
            line = (
                f"{here}{mark} {indent}{short(n.id)} {DIM}{own:>7} ${cost:<7.4f}{RESET} "
                f"{label}{status}"
            )
            typer.echo(line if n.status == "live" else f"{DIM}{line}{RESET}")
            walk(n.id, indent + "  ")

    walk(None, "")
    typer.echo(f"{DIM}* trunk  > current  |  {s.burden_line()}{RESET}")


def cmd_go(s: Session, ref: str) -> None:
    nid = s.resolve(ref)
    if s.log.state.nodes[nid].status != "live":
        raise typer.BadParameter(f"{short(nid)} is not live; restore it first")
    st = s.log.state
    t = graph.trunk(st)
    if t is not None and nid != t and st.trunk_pin != t:
        # Leaving the trunk leaf to branch: pin it, or an equally deep sibling would steal it.
        s.log.append(TrunkPinned(node_id=t))
    s.leaf = nid
    kind = "trunk" if nid == t else "exploration"
    typer.echo(f"at {short(nid)} ({kind}); next input opens a branch here")


def cmd_graft(s: Session, ref: str, depth: Depth, with_tool: list[str]) -> None:
    src = s.resolve(ref)
    edge = ReferenceEdge(src_id=src, dst_id="", depth=depth, include_tool_outputs=with_tool)
    st = s.log.state
    preview = ledger.graft_preview(st, s.leaf, edge, s.provider.count_tokens)
    s.pending.append(edge)
    before = ledger.trunk_burden(st)
    on_trunk = s.leaf == graph.trunk(st)
    typer.echo(
        f"graft {short(src)} -> next node  depth={depth}  "
        + "  ".join(f"{d}: +{ledger.fmt(n)}" for d, n in preview.items())
    )
    after = before + preview[depth] if on_trunk else before
    typer.echo(color("green", ledger.delta(before, after)) + "  (append-only)")
    if with_tool:
        typer.echo(f"including tool outputs: {', '.join(with_tool)}")


def cmd_archive(s: Session, ref: str) -> None:
    nid = _set_status(s, ref, NodeArchived, "archived")
    typer.echo(f"{DIM}restore with /restore {short(nid)}{RESET}")


def cmd_restore(s: Session, ref: str) -> None:
    _set_status(s, ref, NodeRestored, "restored")


def _set_status(
    s: Session, ref: str, event: type[NodeArchived] | type[NodeRestored], word: str
) -> str:
    """Archive or restore a whole subtree (one atomic operation, §2.1) and price it."""
    nid = s.resolve(ref)
    ids = graph.subtree(s.log.state, nid)
    before = ledger.trunk_burden(s.log.state)
    s.log.append(event(ids=ids))
    if s.leaf not in s.log.state.nodes or s.log.state.nodes[s.leaf].status != "live":
        s.leaf = graph.trunk(s.log.state)  # the cursor left with the subtree
    c, why = ledger.cache_color(s.log.state)
    typer.echo(f"{word} {len(ids)} node(s) under {short(nid)}")
    typer.echo(color(c, ledger.delta(before, ledger.trunk_burden(s.log.state))) + f"  ({why})")
    return nid


def cmd_trunk(s: Session, ref: str | None) -> None:
    st = s.log.state
    if ref is None:
        pin = st.trunk_pin
        heur = graph.trunk(st.model_copy(update={"trunk_pin": None}))
        typer.echo(
            f"trunk: {short(graph.trunk(st) or '-')}  pinned: {short(pin) if pin else 'no'}"
            f"  heuristic: {short(heur) if heur else '-'}"
        )
        return
    nid = s.resolve(ref)
    before = ledger.trunk_burden(st)
    s.log.append(TrunkPinned(node_id=nid))
    typer.echo(f"trunk pinned to {short(nid)}")
    typer.echo(
        color("green", ledger.delta(before, ledger.trunk_burden(s.log.state)))
        + "  (no prefix changes)"
    )


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
        raise typer.BadParameter(f"{short(node_id)} has no snapshot (not a git repo?)")
    paths = rewind_paths(state, node_id)
    cur = guard.snapshot(cwd)
    if cur:
        guard.keep(cwd, "undo", cur)
    guard.restore(cwd, n.tree, paths)
    return paths


def cmd_rewind(s: Session, ref: str, mode: str | None) -> None:
    nid = s.resolve(ref)
    paths = rewind_paths(s.log.state, nid)
    if mode is None:  # interactive: show what a code rewind would touch, then ask
        typer.echo(f"files a code rewind would restore: {', '.join(paths) if paths else '(none)'}")
        mode = typer.prompt("rewind [both|code|conversation|cancel]", default="both")
    if mode == "cancel":
        return
    if mode not in ("both", "code", "conversation"):
        raise typer.BadParameter("mode must be both | code | conversation")
    if mode != "code" and s.log.state.nodes[nid].status != "live":
        raise typer.BadParameter(f"{short(nid)} is not live; restore it or rewind code only")
    if mode in ("both", "code"):
        done = rewind_code(s.log.state, s.cwd, nid)
        typer.echo(
            f"restored {len(done)} file(s) to {short(nid)}; previous state at refs/kaipi/undo"
        )
    if mode in ("both", "conversation"):
        cmd_go(s, nid)


def cmd_provider() -> None:
    """Pick a vendor, confirm its endpoint, paste a key: the three things that otherwise
    have to be exported by hand every session. Regional endpoints differ (Moonshot and
    Z.ai each have a .cn and an international one), so the URL is always editable."""
    from kaipi.providers import BUILTIN, auth, save_auth

    current = auth()
    done = current.get("providers", {})
    if done:
        typer.echo(f"{BOLD}已配置的提供商{RESET}")
        for n, saved in done.items():
            models = ", ".join(saved.get("models", [])) or "(还没填模型)"
            typer.echo(f"   · {n:<22} {DIM}{saved.get('base_url', '')}  {models}{RESET}")
        typer.echo(f"{DIM}下面选一个即可新增或修改；切换用哪个模型是 /model 的事{RESET}")

    names = list(BUILTIN)
    typer.echo(f"{BOLD}选择 API 提供商{RESET}")
    for i, n in enumerate(names, 1):
        cfg = BUILTIN[n]
        mark = " (已配置)" if n in done else ""
        url = cfg.base_url or "(官方默认)"
        typer.echo(f"  {i:>2}. {n:<22} {DIM}{cfg.api:<17} {url}{RESET}{mark}")
    pick = typer.prompt("序号", type=int)
    if not 1 <= pick <= len(names):
        raise typer.BadParameter(f"序号要在 1..{len(names)} 之间")
    name = names[pick - 1]
    cfg = BUILTIN[name]

    saved = done.get(name, {})
    base = typer.prompt(
        "API 地址（不同地区可能不同，回车接受）",
        default=saved.get("base_url") or cfg.base_url or "https://api.anthropic.com",
    )
    saved_key = saved.get("api_key", "")
    key = typer.prompt(
        f"{cfg.api_key_env} 的 key" + ("（回车保留已存的）" if saved_key else ""),
        hide_input=True,
        default="" if saved_key else None,
        show_default=False,
    ).strip()
    known = [m for m, pr in ledger.Pricing.load().models.items() if pr.provider == name]
    if known:
        typer.echo(f"{DIM}价格表里这个提供商的模型：{', '.join(known)}{RESET}")
    listed = typer.prompt(
        "你要用的 model id（逗号分隔，可以多个）",
        default=", ".join(saved.get("models", [])) or ", ".join(known[:2]),
    )
    picked = [m.strip() for m in listed.replace("，", ",").split(",") if m.strip()]

    path = save_auth(name, base.strip().rstrip("/"), key, picked)
    typer.echo(f"{GREEN}{name} 已保存到 {path}（权限 600，只有你能读）{RESET}")
    typer.echo(f"{DIM}临时想换 key，export {cfg.api_key_env} 仍然优先于这里{RESET}")
    typer.echo(f"{DIM}接下来用 /model 选一个模型启用{RESET}")


def cmd_model() -> None:
    """Switch models across every provider you configured. One list, so moving from a
    Kimi model to a Claude one is the same gesture as moving between two Kimi ones."""
    from kaipi.providers import active_model, configured_models, use_model

    pairs = configured_models()
    if not pairs:
        typer.echo(
            f"{RED}还没有可选的模型。先跑 /provider 配置一个提供商并列出它的 model id{RESET}"
        )
        return
    now, pricing = active_model(), ledger.Pricing.load()
    typer.echo(f"{BOLD}选择模型{RESET}")
    for i, (who, m) in enumerate(pairs, 1):
        spec = m if m in pricing.models else f"{who}/{m}"
        price = pricing.models.get(m)
        cost = f"in {price.input}/out {price.output} 每百万" if price else "不在价格表，账本按 0 记"
        typer.echo(
            f"  {i:>2}. {m:<26} {DIM}{who:<12} {cost}{RESET}"
            + (f"{GREEN} (当前){RESET}" if spec == now else "")
        )
    pick = typer.prompt("序号", type=int)
    if not 1 <= pick <= len(pairs):
        raise typer.BadParameter(f"序号要在 1..{len(pairs)} 之间")
    who, model = pairs[pick - 1]
    use_model(who, model, model in pricing.models)
    typer.echo(f"{GREEN}已启用 {model}（{who}）{RESET}")
    typer.echo(f"{DIM}当前会话的模型在开始时就冻结了；新开一个会话才会用上{RESET}")


def cmd_ledger(s: Session) -> None:
    r = ledger.report(s.log.state, s.pricing)
    u = r.usage
    typer.echo(f"{BOLD}session {s.log.path.stem}  model {s.log.state.model}{RESET}")
    typer.echo(
        f"total cost: ${r.total_cost:.4f}   tokens: uncached {ledger.fmt(u.input_uncached)}"
        f"  cache write {ledger.fmt(u.cache_write)}  cache read {ledger.fmt(u.cache_read)}"
        f"  output {ledger.fmt(u.output)}"
    )
    typer.echo(
        f"trunk: {short(r.trunk) if r.trunk else '-'}   trunk burden: {ledger.fmt(r.trunk_burden)}"
    )
    if r.branches:
        typer.echo("explorations (one-time cost | tokens kept out of the trunk):")
        for b in r.branches:
            typer.echo(
                f"  {short(b.leaf_id)} {b.status:<9} {b.nodes} node(s)"
                f"  ${b.one_time_cost:.4f} | {ledger.fmt(b.blocked_tokens)}"
            )
    saved = ledger.saved_by_kind(s.log.state, s.pricing)
    labels = {
        "exploration": "探索分支未并入主干",
        "graft": "嫁接带的是结论而非整条分支",
        "interrupt": "打断的回合不进上下文",
    }
    total_tok = sum(t for t, _ in saved.values())
    typer.echo(
        f"{GREEN}saved {ledger.fmt(total_tok)} per trunk turn"
        f" = ${sum(c for _, c in saved.values()):.4f} not spent so far{RESET}"
    )
    for kind, (tok, money) in saved.items():
        if tok:
            typer.echo(f"  {ledger.fmt(tok):>7}/turn  ${money:<8.4f} {DIM}{labels[kind]}{RESET}")
    dropped = sum(n.dropped_thinking for n in s.log.state.nodes.values())
    if dropped:
        typer.echo(f"{RED}thinking blocks dropped by the API (prefix edits): {dropped}{RESET}")


def cmd_sessions(cwd: Path) -> None:
    for p in list_sessions(cwd):
        st = Log(p).state
        typer.echo(f"{p.stem}  {st.model}  {len(st.nodes)} node(s)")


def print_hook(kind: str, t: str) -> None:
    if kind == "text":
        typer.echo(t)
    elif kind == "cmd":
        typer.echo(f"{DIM}$ {t}{RESET}")
    elif kind == "out":
        typer.echo(f"{DIM}{t[:2000]}{RESET}")
    elif kind == "guard":
        typer.echo(f"{RED}exploration branch dirtied the working tree{RESET}")
    elif kind == "stop":
        typer.echo(f"{RED}stopped: {t}{RESET}")
    elif kind == "ledger":
        typer.echo(f"{DIM}{t}{RESET}")


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
    for e in edges:
        if e.depth == "leaf+summary":
            summarize.ensure_summary(s.log, s.cheap, e.src_id, s.leaf)
    s.pending = []
    try:
        nid = agent.run_turn(
            s.log,
            s.provider,
            s.leaf,
            edges,
            text,
            exploration=exploration,
            cwd=s.cwd,
            hook=hook,
            stop=stop,
        )
    except agent.Interrupted as stopped:
        # The cursor goes back to the parent, so the next input opens a sibling rather than
        # continuing from a turn the user threw away; the grafts they staged are theirs, and
        # come back with them rather than being lost with the discarded turn.
        dead = s.log.state.nodes[stopped.node_id]
        s.leaf, s.pending = dead.parent_id, edges
        cost = s.pricing.price(dead.model).cost(dead.usage)
        hook("ledger", f"[{short(dead.id)} 已废弃] ${cost:.4f}  不会进入之后的上下文")
        raise
    if not exploration and st.trunk_pin is not None and st.trunk_pin == s.leaf:
        s.log.append(TrunkPinned(node_id=nid))  # extending the pinned leaf moves the pin
    s.leaf = nid
    n = s.log.state.nodes[nid]
    cost = s.pricing.price(n.model).cost(n.usage)
    kind = "exploration" if exploration else "trunk"
    hook(
        "ledger",
        f"[{short(nid)} {kind}] ${cost:.4f}  cache read "
        f"{ledger.fmt(n.usage.cache_read)}/{ledger.fmt(n.usage.context)}  {s.burden_line()}",
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


def cli_turn(s: Session, text: str, *, explore: bool = False) -> None:
    try:
        with EscWatcher() as stop:
            nid, dirty = run_input(s, text, explore=explore, stop=stop)
    except agent.Interrupted as stopped:
        typer.echo(f"{RED}已停止。这一轮作废，不会进入之后的上下文；花掉的 token 已记账。{RESET}")
        if s.log.state.nodes[stopped.node_id].guard_dirty and typer.confirm(
            "被停掉的探索改动了工作树，回滚吗（git checkout -- . && git clean -fd）?", default=False
        ):
            guard.reset(s.cwd)
        typer.echo(
            f"{DIM}下一句会从 {short(s.leaf) if s.leaf else 'root'} 重新长出一个兄弟节点{RESET}"
        )
        return
    if dirty:
        if typer.confirm(
            "revert working tree (git checkout -- . && git clean -fd)?", default=False
        ):
            guard.reset(s.cwd)
        elif typer.confirm(f"pin trunk to {short(nid)} instead?", default=False):
            cmd_trunk(s, nid)


def interactive(s: Session) -> str:
    """The readline loop. Returns "canvas" when the user hands the session to the canvas."""
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    set_lock(s.cwd, "cli")
    typer.echo(f"kaipi  {s.status()}")
    typer.echo(
        f"{DIM}/tree  /go <id>  /graft <id> [--depth d] [--with-tool id..]  /archive <id>"
        f"  /restore <id>  /rewind <id>  /trunk [pin <id>]  /explore <text>  /ledger"
        f"  /canvas  /provider  /model  /quit   ·  Esc 停止当前回合{RESET}"
    )
    while True:
        try:
            line = input(f"{short(s.leaf) if s.leaf else 'root'}> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if not line:
            continue
        try:
            if line.startswith("/"):
                if line.split()[0] in ("/canvas", "/cli"):
                    s.save()
                    return "canvas"
                if not slash(s, line[1:].split()):
                    return "quit"
            else:
                cli_turn(s, line)
        except (typer.BadParameter, ValueError) as e:
            typer.echo(f"{RED}{e}{RESET}")
        s.save()


def run_surfaces(s: Session, surface: str) -> None:
    """Alternate between the two surfaces until the user quits. Exactly one is open."""
    from kaipi.server import serve

    try:
        while True:
            if surface == "canvas":
                url = serve(s, int(os.environ.get("KAIPI_PORT", "0")))
                typer.echo(f"canvas at {url} closed; session is back in this terminal")
                surface = "cli"
            else:
                if interactive(s) != "canvas":
                    return
                surface = "canvas"
                typer.echo(
                    f"{DIM}canvas opened in your browser; this prompt is closed until the"
                    f" canvas sends /cli (or press Ctrl-C here){RESET}"
                )
    finally:
        s.save()
        release_lock(s.cwd)


def slash(s: Session, argv: list[str]) -> bool:
    verb, args = argv[0], argv[1:]
    match verb:
        case "quit" | "exit":
            return False
        case "tree":
            cmd_tree(s)
        case "go":
            cmd_go(s, args[0])
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
            cmd_graft(s, args[0], depth, tools)
        case "archive":
            cmd_archive(s, args[0])
        case "restore":
            cmd_restore(s, args[0])
        case "trunk":
            cmd_trunk(s, args[1] if len(args) == 2 and args[0] == "pin" else None)
        case "provider":
            cmd_provider()
        case "model":
            cmd_model()
        case "ledger":
            cmd_ledger(s)
        case "explore":
            cli_turn(s, " ".join(args), explore=True)
        case "rewind":
            cmd_rewind(s, args[0], args[1] if len(args) > 1 else None)
        case _:
            typer.echo(f"{RED}unknown command /{verb}{RESET}")
    return True


# --- typer wiring ---------------------------------------------------------------------


def onboard() -> None:
    """Two things stand between a fresh install and a conversation: somewhere to send the
    request, and something to send it to. Ask for each in turn, and say when it is done."""
    from kaipi.providers import BUILTIN, active_model, auth, configured_models

    has_key = bool(auth().get("providers")) or any(
        cfg.api_key_env and os.environ.get(cfg.api_key_env) for cfg in BUILTIN.values()
    )
    if not has_key:
        typer.echo(f"{BOLD}👋 第一次用 kaipi，先花一分钟配两件事。{RESET}")
        typer.echo(f"{DIM}第 1 步 / 共 2 步：选一个 API 提供商，填上地址和 key。{RESET}\n")
        cmd_provider()
        typer.echo("")
    if not (os.environ.get("KAIPI_MODEL") or active_model()):
        if configured_models():
            typer.echo(f"{DIM}第 2 步 / 共 2 步：挑一个模型启用。{RESET}\n")
            cmd_model()
        else:
            typer.echo(f"{RED}还没有可用的模型：再跑一次 /provider，把 model id 填上{RESET}")
            raise typer.Exit(1)
        typer.echo(f"\n{GREEN}🌱 配好了，直接说人话就行——它会自己看代码、跑命令。{RESET}")
        typer.echo(f"{DIM}随时 /tree 看分支，Esc 停掉跑歪的一轮，/ledger 看省了多少。{RESET}\n")


def _session(new: bool = False, *, mutate: bool = True) -> Session:
    if mutate:
        refuse_if_open(Path.cwd())
    elif not list_sessions(Path.cwd()):
        # a read-only command must not conjure an empty session into `kaipi sessions`
        typer.echo(f"{DIM}no session in this directory yet; run `kaipi` to start one{RESET}")
        raise typer.Exit(0)
    return Session(Path.cwd(), new=new)


@app.callback()
def main(
    ctx: typer.Context,
    new: Annotated[bool, typer.Option("--new", help="start a fresh session")] = False,
) -> None:
    """kaipi: interactive session in the current directory (resumes the latest one)."""
    if ctx.invoked_subcommand is None:
        refuse_if_open(Path.cwd())
        onboard()
        run_surfaces(_session(new), "cli")


@app.command()
def canvas(
    new: Annotated[bool, typer.Option("--new", help="start a fresh session")] = False,
) -> None:
    """open the session on the canvas (browser) instead of the terminal."""
    refuse_if_open(Path.cwd())
    run_surfaces(_session(new), "canvas")


@app.command()
def tree() -> None:
    cmd_tree(_session(mutate=False))


@app.command()
def go(node_id: str) -> None:
    s = _session()
    cmd_go(s, node_id)
    s.save()


@app.command()
def graft(
    node_id: str,
    depth: Annotated[str, typer.Option()] = "leaf+summary",
    with_tool: Annotated[list[str] | None, typer.Option("--with-tool")] = None,
) -> None:
    s = _session()
    if depth not in ("leaf", "leaf+summary", "branch"):
        raise typer.BadParameter("depth must be leaf | leaf+summary | branch")
    cmd_graft(s, node_id, depth, with_tool or [])  # type: ignore[arg-type]
    s.save()


@app.command()
def archive(node_id: str) -> None:
    s = _session()
    cmd_archive(s, node_id)
    s.save()


@app.command()
def restore(node_id: str) -> None:
    s = _session()
    cmd_restore(s, node_id)
    s.save()


@app.command()
def rewind(
    node_id: str,
    mode: Annotated[str, typer.Option(help="both | code | conversation")] = "both",
) -> None:
    """rewind code and/or conversation to a node; only agent-touched files are restored."""
    s = _session()
    cmd_rewind(s, node_id, mode)
    s.save()


trunk_app = typer.Typer(invoke_without_command=True)
app.add_typer(trunk_app, name="trunk")


@trunk_app.callback()
def trunk(ctx: typer.Context) -> None:
    """show the trunk; `kaipi trunk pin <id>` re-pins it."""
    if ctx.invoked_subcommand is None:
        cmd_trunk(_session(mutate=False), None)


@trunk_app.command("pin")
def trunk_pin(node_id: str) -> None:
    s = _session()
    cmd_trunk(s, node_id)
    s.save()


@app.command("ledger")
def ledger_cmd() -> None:
    cmd_ledger(_session(mutate=False))


@app.command()
def sessions() -> None:
    cmd_sessions(Path.cwd())


@app.command()
def provider() -> None:
    """configure an API provider: endpoint, key, and the model ids it offers you."""
    cmd_provider()


@app.command()
def model() -> None:
    """switch models across every provider you configured."""
    cmd_model()


if __name__ == "__main__":
    sys.exit(app())
