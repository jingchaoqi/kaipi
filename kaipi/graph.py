"""Tree / DAG queries over State: lineage, LCA, fork point, trunk heuristic (§2.3)."""

from __future__ import annotations

from kaipi.model import Node
from kaipi.store import State


def lineage(state: State, node_id: str) -> list[Node]:
    """Root -> node_id along parent links. Archiving is subtree-atomic, so a live node's
    lineage is entirely live; callers filter on status anyway."""
    out: list[Node] = []
    cur: str | None = node_id
    while cur is not None:
        n = state.nodes[cur]
        out.append(n)
        cur = n.parent_id
    out.reverse()
    return out


def live_lineage(state: State, node_id: str) -> list[Node]:
    return [n for n in lineage(state, node_id) if n.status == "live"]


def children(state: State, node_id: str | None, *, live_only: bool = True) -> list[Node]:
    return [
        n
        for n in state.nodes.values()
        if n.parent_id == node_id and (not live_only or n.status == "live")
    ]


def subtree(state: State, node_id: str) -> list[str]:
    """node_id and every descendant (any status), creation order."""
    ids = {node_id}
    for n in state.nodes.values():  # creation order: parents precede children
        if n.parent_id in ids:
            ids.add(n.id)
    return [i for i in state.nodes if i in ids]


def is_ancestor(state: State, a: str, b: str) -> bool:
    """True if a is on b's lineage (a == b counts)."""
    return any(n.id == a for n in lineage(state, b))


def lca(state: State, a: str, b: str) -> str | None:
    la = {n.id for n in lineage(state, a)}
    for n in reversed(lineage(state, b)):
        if n.id in la:
            return n.id
    return None


def path_below_lca(state: State, node_id: str, leaf_id: str | None) -> list[Node]:
    """The part of node_id's lineage that leaf_id does not already have. This is what
    both a `branch` graft inlines and a `leaf+summary` graft summarises (§2.2)."""
    path = lineage(state, node_id)
    top = lca(state, node_id, leaf_id) if leaf_id else None
    return path[[n.id for n in path].index(top) + 1 :] if top is not None else path


def fork_point(state: State, leaf_id: str) -> str | None:
    """Nearest node on leaf's lineage (leaf included) with >= 2 live children."""
    for n in reversed(live_lineage(state, leaf_id)):
        if len(children(state, n.id)) >= 2:
            return n.id
    return None


def live_leaves(state: State) -> list[Node]:
    return [n for n in state.nodes.values() if n.status == "live" and not children(state, n.id)]


def depth(state: State, node_id: str) -> int:
    return len(live_lineage(state, node_id))


def trunk(state: State) -> str | None:
    """Pinned leaf if still live; else the deepest live leaf, most recently created on ties."""
    pin = state.trunk_pin
    if pin is not None and pin in state.nodes and state.nodes[pin].status == "live":
        return pin
    leaves = live_leaves(state)
    if not leaves:
        return None
    return max(leaves, key=lambda n: (depth(state, n.id), n.seq)).id


def is_exploration(state: State, leaf_id: str | None, *, force: bool = False) -> bool:
    """A turn is an exploration when it is asked for, or when it does not extend the
    trunk. The first turn of a session is always the trunk (§2.3)."""
    return bool(state.nodes) and (force or leaf_id != trunk(state))


def on_trunk(state: State, node_id: str) -> bool:
    t = trunk(state)
    return t is not None and is_ancestor(state, node_id, t)
