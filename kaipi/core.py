"""kaipi's application core: sessions and operations, independent of any interface."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

from pydantic import BaseModel, Field

from kaipi import agent, attachments, context, graph, guard, ledger, providers, summarize, titles
from kaipi.i18n import tr
from kaipi.model import (
    Depth,
    GraftBlock,
    Node,
    NodeAborted,
    NodeArchived,
    NodeCreated,
    NodeRestored,
    ReferenceEdge,
    SessionRenamed,
    SessionStarted,
    TrunkPinned,
    TurnRequest,
    Usage,
    blocks,
    new_id,
)
from kaipi.providers import Provider
from kaipi.store import Log, State, fold, kaipi_dir, list_sessions, sessions_dir

BASE_PROMPT = """You are kaipi, a coding agent working inside the user's repository.
Your only tool is `bash`; use it to inspect, edit (via shell tools) and test code.
Be terse. Stop and reply with a short final answer when the task is done or you are blocked.
Never run destructive git commands unless the user asked for them."""


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


class BusyError(ValueError):
    """The operation would conflict with an executing turn."""


@dataclass(frozen=True)
class Event:
    kind: str
    text: str = ""
    node_id: str = ""
    exploration: bool = False
    retryable: bool = False


Emit = Callable[[Event], None]
P = ParamSpec("P")
T = TypeVar("T")


def idle_operation(
    fn: Callable[Concatenate[Session, P], T],
) -> Callable[Concatenate[Session, P], T]:
    """Serialize idle-only operations and reconcile the cursor selection on failure."""

    @wraps(fn)
    def apply(s: Session, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with s.lock:
            s.require_idle()
            before = s.log, s.leaf, list(s.pending), s.dirty
            try:
                return fn(s, *args, **kwargs)
            except BaseException as err:
                # replace() may have committed before an interrupt reached Python.
                try:
                    committed = s._cursor() == Cursor(
                        session=s.log.path.name, leaf=s.leaf, pending=s.pending
                    )
                except OSError:
                    raise err from None  # do not guess when storage cannot be read
                if not committed:
                    s.log, s.leaf, s.pending, s.dirty = before
                node = s.log.state.nodes.get(s.leaf or "")
                if node is None or node.status != "live":
                    s.leaf = graph.trunk(s.log.state)
                raise

    return apply


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
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.dirty = False
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
        try:
            return Cursor.model_validate_json(self.cursor_path.read_text())
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeError):
            self.cursor_path.rename(self.cursor_path.with_name(f"cursor.corrupt-{new_id()}.json"))
        return Cursor()

    def _place(self) -> None:
        """Leaf and staged grafts for the open log: the cursor's if it points into this
        session, otherwise the trunk leaf."""
        cur = self._cursor()
        mine = cur.session == self.log.path.name
        node = self.log.state.nodes.get(cur.leaf or "") if mine else None
        self.leaf = node.id if node and node.status == "live" else graph.trunk(self.log.state)
        self.pending = cur.pending if mine else []

    def _switch(self, path: Path) -> None:
        """`/resume` inside a session: open another log in place."""
        self.log = Log(path)
        self._place()
        self.dirty = False
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
        with self.lock:
            kaipi_dir(self.cwd)
            cur = Cursor(session=self.log.path.name, leaf=self.leaf, pending=self.pending)
            fd, temporary = tempfile.mkstemp(prefix=".cursor-", dir=self.cursor_path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(cur.model_dump_json())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.cursor_path)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _build(self, model: str) -> Provider:
        from kaipi.providers import ProviderConfig, build

        price = self.pricing.find(model)
        cfgs = {k: ProviderConfig(**v) for k, v in self.pricing.providers.items()}
        # A qualified model chooses its provider; a bare model uses the price row.
        return build(
            model,
            cfgs,
            provider_of=None if "/" in model else (price.provider if price else None),
            adaptive_thinking=price.adaptive_thinking if price else True,
        )

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
            except ValueError:
                self._cheap = self.provider
            self._cheap_for = spec
        return self._cheap

    def resolve(self, ref: str) -> str:
        found = self.log.state.find(ref)
        if found is None:
            raise ValueError(tr("{ref}: 没有这个节点（用 /tree 里的编号，如 N3）", ref=ref))
        return found

    def name(self, node_id: str | None) -> str:
        return self.log.state.label(node_id)

    def _validate_pending(self, leaf: str | None, state: State | None = None) -> None:
        for edge in self.pending:
            context.render_graft(self.log.state if state is None else state, edge, leaf)

    @idle_operation
    def go(self, ref: str) -> str:
        nid = self.resolve(ref)
        if self.log.state.nodes[nid].status != "live":
            raise ValueError(tr("{v0} is not live; restore it first", v0=self.name(nid)))
        self._validate_pending(nid)
        st = self.log.state
        trunk = graph.trunk(st)
        if trunk is not None and nid != trunk and st.trunk_pin != trunk:
            self.log.append(TrunkPinned(node_id=trunk))
        self.leaf = nid
        self.save()
        return nid

    def preview(self, src: str, tools: list[str]) -> dict[str, Any]:
        edge = ReferenceEdge(src_id=self.resolve(src), dst_id="", include_tool_outputs=tools)
        st = self.log.state
        return {
            "sizes": ledger.graft_preview(st, self.leaf, edge, self.provider.count_tokens),
            "burden": ledger.trunk_burden(st),
            "on_trunk": self.leaf == graph.trunk(st),
        }

    @idle_operation
    def graft(self, ref: str, depth: Depth, tools: list[str]) -> dict[Depth, int]:
        src = self.resolve(ref)
        edge = ReferenceEdge(src_id=src, dst_id="", depth=depth, include_tool_outputs=tools)
        sizes = ledger.graft_preview(self.log.state, self.leaf, edge, self.provider.count_tokens)
        self.pending.append(edge)
        self.save()
        return sizes

    @idle_operation
    def set_status(self, ref: str, *, restore: bool) -> list[str]:
        ids = graph.subtree(self.log.state, self.resolve(ref))
        event = NodeRestored(ids=ids) if restore else NodeArchived(ids=ids)
        state = fold([*self.log.events, event])
        node = state.nodes.get(self.leaf or "")
        leaf = node.id if node and node.status == "live" else graph.trunk(state)
        self._validate_pending(leaf, state)
        self.log.append(event)
        self.leaf = leaf
        self.save()
        return ids

    @idle_operation
    def pin(self, ref: str) -> str:
        nid = self.resolve(ref)
        if self.log.state.nodes[nid].status == "live":
            self._validate_pending(nid)
        self.log.append(TrunkPinned(node_id=nid))
        if graph.trunk(self.log.state) == nid:
            self.leaf = nid
        self.save()
        return nid

    @idle_operation
    def rewind(self, ref: str, mode: str) -> tuple[str, list[str]]:
        nid = self.resolve(ref)
        if mode not in ("both", "code", "conversation"):
            raise ValueError(tr("mode must be both | code | conversation"))
        if mode != "code" and self.log.state.nodes[nid].status != "live":
            raise ValueError(
                tr("{v0} is not live; restore it or rewind code only", v0=self.name(nid))
            )
        if mode != "code":
            self._validate_pending(nid)
        paths = rewind_code(self.log.state, self.cwd, nid) if mode != "conversation" else []
        if mode != "code":
            self.go(nid)
        self.dirty = False
        return nid, paths

    @idle_operation
    def new(self) -> None:
        previous = self.log
        self._switch(self._start().path)
        if not previous.state.nodes:
            try:
                previous.path.unlink(missing_ok=True)
            except OSError:
                pass  # housekeeping must not undo a successfully persisted switch

    @idle_operation
    def resume(self, ref: str) -> bool:
        path = find_session(self.cwd, ref)
        if path == self.log.path:
            return False
        self._switch(path)
        return True

    @idle_operation
    def rename(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError(tr("用法：/rename <名字>"))
        self.log.append(SessionRenamed(name=name))

    @idle_operation
    def stage(self, edges: list[ReferenceEdge]) -> None:
        resolved = [
            edge.model_copy(update={"src_id": self.resolve(edge.src_id)}, deep=True)
            for edge in edges
        ]
        for edge in resolved:
            context.render_graft(self.log.state, edge, self.leaf)
        self.pending = resolved
        self.save()

    @idle_operation
    def reset(self) -> None:
        guard.reset(self.cwd)
        self.dirty = False

    @idle_operation
    def refresh_configuration(self) -> None:
        self.pricing = ledger.Pricing.load()
        self._provider = self._cheap = None

    def require_idle(self) -> None:
        if self.busy:
            raise BusyError(tr("a turn is running"))

    @property
    def busy(self) -> bool:
        return not self.idle.is_set()

    def stop(self) -> None:
        self.stopping.set()

    def claim(self, stop: threading.Event | None = None) -> None:
        with self.lock:
            self.require_idle()
            self.stopping = stop if stop is not None else threading.Event()
            self.idle.clear()

    def retry_node(self) -> Node:
        """Validate the latest attempt without changing the cursor or acquiring execution."""
        with self.lock:
            self.require_idle()
            node = next(reversed(self.log.state.nodes.values()), None)
            if node is None:
                raise ValueError(tr("没有可重试的原始请求"))
            node = node.model_copy(deep=True)
            if node.request is None:
                # Old logs already contain the submitted blocks and materialised graft text.
                before = fold([e for e in self.log.events if e.seq < node.seq])
                edges = [
                    e.model_copy(update={"dst_id": ""}) for e in self.log.state.edges_into(node.id)
                ]
                for message in node.payload:
                    content = blocks(message["content"])
                    if message["role"] != "user" or not content or content[0].get("type") != "text":
                        continue
                    if context.classify(content[0]["text"]) == "graft":
                        if not node.grafts and len(content) == len(edges):
                            node.grafts = [
                                GraftBlock(
                                    src_id=e.src_id,
                                    depth=e.depth,
                                    include_tool_outputs=e.include_tool_outputs,
                                    text=b["text"],
                                )
                                for e, b in zip(edges, content, strict=True)
                            ]
                        continue
                    node.request = TurnRequest(
                        input={
                            "role": "user",
                            "content": [
                                b
                                for b in content
                                if b.get("type") != "text" or context.classify(b["text"]) != "guard"
                            ],
                        },
                        edges=edges,
                        exploration=any(b.get("text") == context.GUARD_TEXT for b in content),
                        trunk_id=graph.trunk(before),
                    )
                    break
                if node.request is None or (edges and not node.grafts):
                    raise ValueError(tr("没有可重试的原始请求"))
            request = node.request
            position = node.id if node.completed else node.parent_id
            trunk = node.id if node.completed and not request.exploration else request.trunk_id
            pending = [] if node.completed else request.edges
            if (
                node.status not in ("live", "aborted", "tombstone")
                or self.leaf != position
                or graph.trunk(self.log.state) != trunk
                or self.pending != pending
            ):
                raise ValueError(tr("会话位置或待嫁接内容已变化，无法重试上一轮"))
            return node

    @property
    def can_retry(self) -> bool:
        try:
            self.retry_node()
            return True
        except ValueError:
            return False

    def retry(
        self, emit: Emit = lambda _: None, *, stop: threading.Event | None = None
    ) -> tuple[str, bool]:
        """Re-run only the latest attempt, using the current selected model."""
        return run_input(self, "", emit, stop=stop, retry=True)

    def start_retry(self, emit: Emit) -> threading.Thread:
        return self.start("", emit, retry=True)

    def start(
        self, text: str, emit: Emit, *, explore: bool = False, retry: bool = False
    ) -> threading.Thread:
        """Reserve execution before returning, so another client cannot race the worker."""
        with self.lock:
            target = self.retry_node() if retry else None
            self.claim()

        def work() -> None:
            try:
                _execute(self, text, emit, explore=explore, stop=self.stopping, retry=target)
            except Exception:  # noqa: BLE001 - _execute publishes failure before releasing idle
                pass

        thread = threading.Thread(target=work, daemon=True)
        try:
            thread.start()
        except BaseException:
            self.idle.set()
            raise
        return thread

    def inspect_context(self, placeholder: str) -> context.Context:
        with self.lock:
            st, leaf, pending = self.log.state, self.leaf, list(self.pending)
        return context.build(st, leaf, pending, placeholder, guard=graph.is_exploration(st, leaf))

    @idle_operation
    def export_context(self, placeholder: str) -> Path:
        from kaipi.context_export import export

        return export(self, placeholder)

    def state(self) -> dict[str, Any]:
        # Log.append replaces State; retain that version with its cursor before rendering.
        with self.lock:
            session, st, pricing = self.log.path.stem, self.log.state, self.pricing
            leaf, pending = self.leaf, [e.model_dump() for e in self.pending]
            busy, dirty = self.busy, self.dirty
            status = self.status_data(st)
        t = graph.trunk(st)
        nodes = []
        for n in st.nodes.values():
            nodes.append(
                {
                    "id": n.id,
                    "label": st.label(n.id),
                    "parent_id": n.parent_id,
                    "status": n.status,
                    "seq": n.seq,
                    "input": context.user_input(n),
                    "answer": context.final_answer(n),
                    "attachments": context.has_attachments(n),
                    "own": ledger.own_tokens(st, n.id),
                    "ctx": n.context_tokens,
                    "tools": [{"id": i, "cmd": c} for i, c in context.tool_calls(n)],
                    "grafts": [g.src_id for g in n.grafts],
                    "cost": pricing.price(n.model).cost(n.usage),
                    "dropped": n.dropped_thinking,
                }
            )
        return {
            "session": session,
            "model": st.model,
            "nodes": nodes,
            "edges": [e.model_dump() for e in st.edges],
            "trunk": t,
            "pin": st.trunk_pin,
            "leaf": leaf,
            "pending": pending,
            "burden": status["burden"],
            "cost": status["cost"],
            "busy": busy,
            "dirty": dirty,
            "repo": guard.is_repo(self.cwd),
            "status": status,
            "retryable": self.can_retry,
        }

    def status_data(self, st: State | None = None) -> dict[str, Any]:
        """Shared status figures; interfaces choose their own layout and visibility."""
        st = self.log.state if st is None else st
        spec = self.wanted_model()
        price = self.pricing.price(spec)
        who, model = self.pricing.split(spec)
        saved = ledger.saved_by_kind(st, self.pricing)
        return {
            "provider": who,
            "model": model,
            "cwd": str(self.cwd),
            "window": price.context,
            "burden": ledger.trunk_burden(st),
            "cost": ledger.total_cost(st, self.pricing),
            "saved": {k: {"tokens": t, "cost": c} for k, (t, c) in saved.items()},
        }


def find_session(cwd: Path, ref: str) -> Path:
    hits = [
        p
        for p in list_sessions(cwd)
        if p.stem == ref or p.stem.endswith(ref) or p.stem.startswith(ref)
    ]
    if len(hits) != 1:
        raise ValueError(
            f"{ref}: {tr('没有这个会话') if not hits else tr('匹配到多个会话，请输入更长的 ID')}"
        )
    return hits[0]


def session_title(st: State) -> str:
    """What a session is about: its name, else the question that opened it."""
    if st.name:
        return st.name
    first = next((context.user_input(n) for n in st.nodes.values() if n.parent_id is None), "")
    return first.splitlines()[0][:40] if first else tr("(空对话)")


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
        raise ValueError(tr("{v0} has no snapshot (not a git repo?)", v0=state.label(node_id)))
    paths = rewind_paths(state, node_id)
    cur = guard.snapshot(cwd)
    if cur:
        guard.keep(cwd, "undo", cur)
    guard.restore(cwd, n.tree, paths)
    return paths


def _run_input(
    s: Session,
    text: str,
    emit: Emit,
    *,
    explore: bool = False,
    stop: threading.Event | None = None,
    retry: Node | None = None,
) -> tuple[str, bool]:
    """Run one turn under the cursor. Returns (node id, working tree dirtied).
    `explore` forces an exploration even from the trunk leaf (the trunk is pinned in place).
    `stop` lets a surface interrupt the turn; agent.Interrupted then reaches the caller."""

    def hook(kind: str, text: str) -> None:
        emit(Event(kind, text))

    st = s.log.state
    t = graph.trunk(st)
    leaf = retry.parent_id if retry else s.leaf
    if retry:
        assert retry.request is not None
        text = attachments.Input.from_blocks(retry.request.input)
    exploration = (
        retry.request.exploration
        if retry and retry.request
        else graph.is_exploration(st, leaf, force=explore)
    )
    edges = retry.request.edges if retry and retry.request else s.pending
    request = TurnRequest(
        input=context.input_message(text), edges=edges, exploration=exploration, trunk_id=t
    )
    try:
        emit(Event("start", str(text), exploration=exploration))
        if s.pricing.find(s.wanted_model()) is None:
            emit(Event("unpriced", s.wanted_model()))
        with s.lock:
            if exploration and leaf == t and st.trunk_pin != t:
                s.log.append(TrunkPinned(node_id=t))
        use: list[ReferenceEdge] = []
        frozen = retry.grafts if retry and (retry.grafts or not edges) else None
        for edge in edges:
            if frozen is None and edge.depth == "leaf+summary":
                try:
                    summarize.ensure_summary(s.log, s.cheap, edge.src_id, leaf)
                except Exception as err:  # noqa: BLE001 - a summary is optional
                    edge = edge.model_copy(update={"depth": "leaf"})
                    emit(Event("summary_failed", providers.redact(str(err)), edge.src_id))
            use.append(edge)
        nid = agent.run_turn(
            s.log,
            s.provider,
            leaf,
            use,
            text,
            exploration=exploration,
            cwd=s.cwd,
            hook=hook,
            stop=stop,
            request=request,
            grafts=frozen,
        )
    except (Exception, KeyboardInterrupt) as err:
        with s.lock:
            dead = next(reversed(s.log.state.nodes.values()), None)
            if dead is None or dead.seq < st.next_seq:
                # Preparation failed before the agent could create its node.
                nid = new_id()
                s.log.append(
                    NodeCreated(id=nid, parent_id=leaf, model=s.wanted_model(), request=request)
                )
                dead = s.log.state.nodes[nid]
            if dead.status != "aborted":
                s.log.append(
                    NodeAborted(
                        id=dead.id, payload=[request.input], usage=Usage(), guard_dirty=s.dirty
                    )
                )
                dead = s.log.state.nodes[dead.id]
            s.leaf, s.pending, s.dirty = dead.parent_id, list(request.edges), dead.guard_dirty
        emit(
            Event(
                "aborted",
                ""
                if isinstance(err, (agent.Interrupted, KeyboardInterrupt))
                else providers.redact(str(err)),
                node_id=dead.id,
                retryable=True,
            )
        )
        if isinstance(err, (agent.Interrupted, agent.Failed)):
            raise
        if isinstance(err, KeyboardInterrupt):
            raise agent.Interrupted(dead.id, dead.usage) from None
        raise agent.Failed(
            providers.redact(f"{type(err).__name__}: {err}").split("\n")[0][:300]
        ) from None
    n = s.log.state.nodes[nid]
    with s.lock:
        if not exploration and (retry or (st.trunk_pin is not None and st.trunk_pin == s.leaf)):
            s.log.append(TrunkPinned(node_id=nid))  # extending the pinned leaf moves the pin
        s.leaf, s.pending, s.dirty = nid, [], n.guard_dirty
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
    emit(Event("completed", node_id=nid, exploration=exploration))
    return nid, n.guard_dirty


def _execute(
    s: Session,
    text: str,
    emit: Emit,
    *,
    explore: bool,
    stop: threading.Event,
    retry: Node | None = None,
) -> tuple[str, bool]:
    finished: Event | None = None

    def forward(ev: Event) -> None:
        nonlocal finished
        if ev.kind in ("completed", "aborted"):
            finished = ev
        else:
            emit(ev)

    try:
        if not text.strip() and retry is None:
            raise ValueError(tr("请输入问题或添加附件"))
        return _run_input(s, text, forward, explore=explore, stop=stop, retry=retry)
    except Exception as err:
        if finished is None:
            finished = Event("error", providers.redact(str(err)))
        raise
    finally:
        with s.lock:
            try:
                try:
                    s.save()
                except Exception as err:
                    emit(Event("error", providers.redact(str(err))))
                    raise
                if finished:
                    emit(finished)
            finally:
                # A completion callback must return before a new turn or handoff can start.
                s.idle.set()


def run_input(
    s: Session,
    text: str,
    emit: Emit = lambda _: None,
    *,
    explore: bool = False,
    stop: threading.Event | None = None,
    retry: bool = False,
) -> tuple[str, bool]:
    with s.lock:
        target = s.retry_node() if retry else None
        s.claim(stop)
    return _execute(s, text, emit, explore=explore, stop=s.stopping, retry=target)


def input_text(text: str, items: list[dict[str, Any]]) -> str:
    files = [attachments.Attachment.model_validate(item) for item in items]
    return attachments.Input(text, files) if files else text
