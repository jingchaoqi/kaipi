"""JSONL event log and the pure fold that turns it into session state (§4)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from kaipi.model import (
    EdgeAdded,
    Event,
    Node,
    NodeArchived,
    NodeCompleted,
    NodeCreated,
    NodeRestored,
    NodeTombstoned,
    ReferenceEdge,
    SessionStarted,
    SummaryGenerated,
    TrunkPinned,
    Usage,
)

_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class State(BaseModel):
    session_id: str = ""
    cwd: str = ""
    model: str = ""
    system_prompt: str = ""
    nodes: dict[str, Node] = Field(default_factory=dict)  # insertion order == creation order
    edges: list[ReferenceEdge] = Field(default_factory=list)
    trunk_pin: str | None = None
    next_seq: int = 0
    # (model, usage) per generated graft summary: real spend that belongs to no node
    summaries: list[tuple[str, Usage]] = Field(default_factory=list)

    def edges_into(self, dst_id: str) -> list[ReferenceEdge]:
        return [e for e in self.edges if e.dst_id == dst_id]


def fold(events: list[Event]) -> State:
    """Pure: the same event list always yields the same State (invariant 6)."""
    s = State()
    for ev in events:
        s.next_seq = max(s.next_seq, ev.seq + 1)
        match ev:
            case SessionStarted():
                s.session_id, s.cwd, s.model = ev.session_id, ev.cwd, ev.model
                s.system_prompt = ev.system_prompt
            case NodeCreated():
                s.nodes[ev.id] = Node(
                    id=ev.id, parent_id=ev.parent_id, created_at=ev.ts, seq=ev.seq, model=ev.model
                )
            case NodeCompleted():
                n = s.nodes[ev.id]
                n.payload, n.grafts, n.usage = ev.payload, ev.grafts, ev.usage
                n.context_tokens, n.dropped_thinking = ev.context_tokens, ev.dropped_thinking
                n.tree, n.paths, n.guard_dirty = ev.tree, list(ev.paths), ev.guard_dirty
                n.completed = True
            case EdgeAdded():
                s.edges.append(ev.edge)
            case NodeArchived():
                for i in ev.ids:
                    if s.nodes[i].status == "live":
                        s.nodes[i].status = "archived"
            case NodeRestored():
                for i in ev.ids:
                    if s.nodes[i].status == "archived":
                        s.nodes[i].status = "live"
            case NodeTombstoned():
                for i in ev.ids:
                    s.nodes[i].status = "tombstone"
                    s.nodes[i].payload, s.nodes[i].grafts = [], []
            case TrunkPinned():
                s.trunk_pin = ev.node_id
            case SummaryGenerated():
                s.nodes[ev.node_id].summary = ev.summary
                s.summaries.append((ev.model, ev.usage))
    # A node created but never completed (crash, Ctrl-C) is a tombstone: no half-frozen payloads.
    for n in s.nodes.values():
        if not n.completed and n.status != "tombstone":
            n.status = "tombstone"
    return s


def dumps(ev: Event) -> str:
    return json.dumps(
        _event_adapter.dump_python(ev, mode="json"), sort_keys=True, ensure_ascii=False
    )


def loads(line: str) -> Event:
    return _event_adapter.validate_json(line)


class Log:
    """Append-only JSONL file. `append` stamps `seq` and keeps `state` in sync."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[Event] = []
        if path.exists():
            with path.open(encoding="utf-8") as f:
                self.events = [loads(line) for line in f if line.strip()]
        self.state = fold(self.events)

    def append(self, ev: Event) -> None:
        ev.seq = self.state.next_seq
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(dumps(ev) + "\n")
        self.events.append(ev)
        self.state = fold(self.events)


def kaipi_dir(cwd: Path) -> Path:
    """`.kaipi/` holds session logs, the cursor and the snapshot index. It ignores itself, so
    a kaipi session never shows up as untracked noise in the user's `git status`."""
    d = cwd / ".kaipi"
    if not d.exists():
        d.mkdir(parents=True)
        (d / ".gitignore").write_text("*\n")
    return d


def sessions_dir(cwd: Path) -> Path:
    return kaipi_dir(cwd) / "sessions"


def list_sessions(cwd: Path) -> list[Path]:
    d = cwd / ".kaipi" / "sessions"  # read-only: never create the directory just to look
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if d.exists() else []
