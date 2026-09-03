"""Data model: nodes, edges, usage, and the append-only event log (CONCEPTS.md §2, §4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from ulid import ULID

Status = Literal["live", "archived", "tombstone"]
Depth = Literal["leaf", "leaf+summary", "branch"]
Message = dict[str, Any]  # one API message: {"role": ..., "content": [...]} with raw blocks


def new_id() -> str:
    return str(ULID())


def now() -> datetime:
    return datetime.now(UTC)


class Usage(BaseModel):
    input_uncached: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_uncached=self.input_uncached + other.input_uncached,
            cache_write=self.cache_write + other.cache_write,
            cache_read=self.cache_read + other.cache_read,
            output=self.output + other.output,
        )

    @property
    def context(self) -> int:
        """Everything the model read on this request: the size of the context."""
        return self.input_uncached + self.cache_write + self.cache_read


class GraftBlock(BaseModel):
    """A materialised reference edge, snapshotted into the destination node."""

    src_id: str
    depth: Depth
    include_tool_outputs: list[str] = Field(default_factory=list)
    text: str


class ReferenceEdge(BaseModel):
    src_id: str
    dst_id: str
    depth: Depth = "leaf+summary"
    include_tool_outputs: list[str] = Field(default_factory=list)


class Node(BaseModel):
    id: str
    parent_id: str | None
    created_at: datetime
    seq: int  # position of node_created in the log; ordering authority for invariant 2
    status: Status = "live"
    model: str
    payload: list[Message] = Field(default_factory=list)
    grafts: list[GraftBlock] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)  # summed over every request of the turn
    context_tokens: int = 0  # context size measured by the API on the turn's last request
    tree: str | None = None  # git tree of the working tree when the turn ended (rewind target)
    paths: list[str] = Field(default_factory=list)  # files this turn changed on disk
    dropped_thinking: int = 0  # thinking blocks the API dropped (prefix-binding mismatch)
    summary: str | None = None
    completed: bool = False


# --- events -----------------------------------------------------------------


class _Ev(BaseModel):
    seq: int = 0
    ts: datetime = Field(default_factory=now)


class SessionStarted(_Ev):
    type: Literal["session_started"] = "session_started"
    session_id: str
    cwd: str
    model: str
    system_prompt: str  # frozen for the whole session (AGENTS.md is snapshotted here)


class NodeCreated(_Ev):
    type: Literal["node_created"] = "node_created"
    id: str
    parent_id: str | None
    model: str


class NodeCompleted(_Ev):
    type: Literal["node_completed"] = "node_completed"
    id: str
    payload: list[Message]
    grafts: list[GraftBlock] = Field(default_factory=list)
    usage: Usage
    context_tokens: int = 0
    dropped_thinking: int = 0
    tree: str | None = None
    paths: list[str] = Field(default_factory=list)


class EdgeAdded(_Ev):
    type: Literal["edge_added"] = "edge_added"
    edge: ReferenceEdge


class NodeArchived(_Ev):
    type: Literal["node_archived"] = "node_archived"
    ids: list[str]  # the whole subtree, resolved when the command ran (atomic)


class NodeRestored(_Ev):
    type: Literal["node_restored"] = "node_restored"
    ids: list[str]


class NodeTombstoned(_Ev):
    type: Literal["node_tombstoned"] = "node_tombstoned"
    ids: list[str]


class TrunkPinned(_Ev):
    type: Literal["trunk_pinned"] = "trunk_pinned"
    node_id: str | None


class SummaryGenerated(_Ev):
    type: Literal["summary_generated"] = "summary_generated"
    node_id: str
    summary: str
    model: str = ""  # the cheap model, priced separately from the node's own model
    usage: Usage = Field(default_factory=Usage)


Event = Annotated[
    SessionStarted
    | NodeCreated
    | NodeCompleted
    | EdgeAdded
    | NodeArchived
    | NodeRestored
    | NodeTombstoned
    | TrunkPinned
    | SummaryGenerated,
    Field(discriminator="type"),
]
