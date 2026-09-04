"""Context assembly (§2.4). Pure: (leaf, reference edges, archived set) -> request."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kaipi import graph
from kaipi.model import GraftBlock, Message, Node, ReferenceEdge, blocks, text_of, texts
from kaipi.store import State

GRAFT_TAG = "<kaipi:graft"
GUARD_TAG = "<kaipi:guard>"
GUARD_TEXT = (
    f"{GUARD_TAG}This branch is an exploration: it is read-only. Inspect and reason, but do "
    "not create, modify or delete files, and do not run commands that change the working "
    "tree.</kaipi:guard>"
)


class Context(BaseModel):
    system: str
    messages: list[Message]
    cache_points: list[int] = Field(default_factory=list)  # message indices (fork, lineage end)
    grafts: list[GraftBlock] = Field(default_factory=list)
    payload_start: int  # index in `messages` where the new node's own payload begins


# --- reading a frozen node -------------------------------------------------


def classify(text: str) -> str:
    """What a user-role text block is: the human's input, a graft block, or the guard note.
    The tags are the only thing that distinguishes them, so only this function reads them."""
    if text.startswith(GRAFT_TAG):
        return "graft"
    return "guard" if text.startswith(GUARD_TAG) else "user"


def user_input(node: Node) -> str:
    for m in node.payload:
        if m["role"] != "user":
            continue
        said = [t for t in texts(m["content"]) if classify(t) != "guard"]
        if said and classify(said[0]) == "user":
            return "\n".join(said)
    return ""


def final_answer(node: Node) -> str:
    for m in reversed(node.payload):
        if m["role"] == "assistant":
            return text_of(m["content"])
    return ""


def tool_calls(node: Node) -> list[tuple[str, str]]:
    """(tool_use id, command) for every bash call in the turn."""
    out: list[tuple[str, str]] = []
    for m in node.payload:
        if m["role"] == "assistant":
            for b in blocks(m["content"]):
                if b.get("type") == "tool_use":
                    out.append((b["id"], str(b.get("input", {}).get("command", ""))))
    return out


def tool_outputs(node: Node, ids: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in node.payload:
        if m["role"] == "user":
            for b in blocks(m["content"]):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in ids:
                    out.append((b["tool_use_id"], text_of(b.get("content", ""))))
    return out


# --- grafts -------------------------------------------------------------------


def leaf_text(n: Node) -> str:
    return f"[user]\n{user_input(n)}\n[assistant]\n{final_answer(n)}"


def render_graft(state: State, edge: ReferenceEdge, leaf_id: str | None) -> GraftBlock:
    src = state.nodes[edge.src_id]
    if src.status == "tombstone":
        raise ValueError(f"cannot graft tombstoned node {edge.src_id}")
    if leaf_id is not None and graph.is_ancestor(state, edge.src_id, leaf_id):
        raise ValueError(f"{edge.src_id} is already in this lineage; nothing to graft")
    parts: list[str] = [f"{GRAFT_TAG} src={src.id} depth={edge.depth}>"]
    if edge.depth == "branch":
        for n in graph.path_below_lca(state, src.id, leaf_id):
            parts.append(f"<node id={n.id}>\n{leaf_text(n)}\n</node>")
    else:
        parts.append(leaf_text(src))
        if edge.depth == "leaf+summary":
            parts.append(f"[path summary]\n{src.summary or '(no summary available)'}")
    for tid, out in tool_outputs(src, edge.include_tool_outputs):
        parts.append(f"<tool_output id={tid}>\n{out}\n</tool_output>")
    parts.append("</kaipi:graft>")
    return GraftBlock(
        src_id=src.id,
        depth=edge.depth,
        include_tool_outputs=list(edge.include_tool_outputs),
        text="\n".join(parts),
    )


def graft_message(grafts: list[GraftBlock]) -> Message:
    """All grafts in ONE user message so they occupy a single cache position."""
    return {"role": "user", "content": [{"type": "text", "text": g.text} for g in grafts]}


def input_message(text: str, *, guard: bool) -> Message:
    blocks: list[dict[str, str]] = []
    if guard:
        blocks.append({"type": "text", "text": GUARD_TEXT})
    blocks.append({"type": "text", "text": text})
    return {"role": "user", "content": blocks}


# --- assembly -------------------------------------------------------------------


def build(
    state: State,
    leaf_id: str | None,
    edges: list[ReferenceEdge],
    text: str,
    *,
    guard: bool = False,
) -> Context:
    messages: list[Message] = []
    points: set[int] = set()  # the two moving cache breakpoints: fork point, lineage end (§7)
    if leaf_id is not None:
        if state.nodes[leaf_id].status != "live":
            raise ValueError(f"{leaf_id} is not live")
        fork = graph.fork_point(state, leaf_id)
        for n in graph.live_lineage(state, leaf_id):
            messages.extend(n.payload)
            if n.id in (fork, leaf_id):
                points.add(len(messages) - 1)
    grafts = [render_graft(state, e, leaf_id) for e in edges]
    payload_start = len(messages)
    if grafts:
        messages.append(graft_message(grafts))
    messages.append(input_message(text, guard=guard))
    return Context(
        system=state.system_prompt,
        messages=messages,
        cache_points=sorted(p for p in points if p >= 0),
        grafts=grafts,
        payload_start=payload_start,
    )
