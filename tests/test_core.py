from __future__ import annotations

import json

import pytest

from kaipi import context, graph
from kaipi.model import NodeArchived, NodeCreated, NodeRestored, ReferenceEdge, TrunkPinned
from kaipi.store import Log, dumps, fold
from tests.conftest import Builder

# --- the six invariants (§2.5) ---------------------------------------------------


def test_inv1_every_node_has_exactly_one_parent(log: Log, tree: dict[str, str]) -> None:
    for n in log.state.nodes.values():
        assert n.parent_id is None or n.parent_id in log.state.nodes
    assert sum(n.parent_id is None for n in log.state.nodes.values()) == 1


def test_inv2_reference_edges_point_to_earlier_nodes(
    log: Log, b: Builder, tree: dict[str, str]
) -> None:
    dst = b.node(tree["a1"], "merge", edges=[ReferenceEdge(src_id=tree["b"], dst_id="?")])
    for e in log.state.edges:
        assert log.state.nodes[e.src_id].seq < log.state.nodes[e.dst_id].seq
    assert log.state.edges_into(dst)[0].src_id == tree["b"]


def test_inv3_graft_is_a_snapshot(log: Log, tree: dict[str, str]) -> None:
    edge = ReferenceEdge(src_id=tree["b"], dst_id="new", depth="leaf")
    ctx = context.build(log.state, tree["a1"], [edge], "merge b")
    snapshot = ctx.grafts[0].text
    log.append(NodeArchived(ids=graph.subtree(log.state, tree["b"])))
    assert log.state.nodes[tree["b"]].status == "archived"
    # the block rendered earlier is unaffected; rendering again from the archived src still works
    assert "b says hi" in snapshot
    assert context.render_graft(log.state, edge, tree["a1"]).text == snapshot


def test_inv4_trunk_only_receives_explicit_grafts(log: Log, tree: dict[str, str]) -> None:
    log.append(NodeArchived(ids=graph.subtree(log.state, tree["b"])))
    ctx = context.build(log.state, graph.trunk(log.state) or "", [], "next")
    assert "b says hi" not in json.dumps(ctx.messages)


def test_inv5_exploration_guard_is_in_the_input(log: Log, tree: dict[str, str]) -> None:
    ctx = context.build(log.state, tree["b"], [], "look around", guard=True)
    first = ctx.messages[-1]["content"][0]["text"]
    assert first.startswith(context.GUARD_TAG)
    assert (
        context.user_input(log.state.nodes[tree["b"]]) == "try b"
    )  # guard never leaks into inputs


def test_inv6_context_is_deterministic(log: Log, tree: dict[str, str]) -> None:
    edges = [ReferenceEdge(src_id=tree["b"], dst_id="x", depth="branch")]
    a = context.build(log.state, tree["a1"], edges, "hi").model_dump_json()
    b_ = context.build(fold(log.events), tree["a1"], edges, "hi").model_dump_json()
    assert a == b_


# --- context assembly --------------------------------------------------------------


def test_grafts_sit_after_lineage_before_input(log: Log, tree: dict[str, str]) -> None:
    edges = [ReferenceEdge(src_id=tree["b"], dst_id="x", depth="leaf")]
    ctx = context.build(log.state, tree["a1"], edges, "merge")
    lineage_len = sum(len(n.payload) for n in graph.live_lineage(log.state, tree["a1"]))
    assert ctx.payload_start == lineage_len
    assert ctx.messages[lineage_len]["content"][0]["text"].startswith(context.GRAFT_TAG)
    assert ctx.messages[-1]["content"][-1]["text"] == "merge"
    assert ctx.messages[:lineage_len] == [
        m for n in graph.live_lineage(log.state, tree["a1"]) for m in n.payload
    ]


def test_multiple_grafts_occupy_one_message(log: Log, tree: dict[str, str]) -> None:
    edges = [
        ReferenceEdge(src_id=tree["b"], dst_id="x", depth="leaf"),
        ReferenceEdge(src_id=tree["b"], dst_id="x", depth="branch"),
    ]
    ctx = context.build(log.state, tree["a1"], edges, "merge")
    assert len(ctx.messages) == ctx.payload_start + 2
    assert len(ctx.messages[ctx.payload_start]["content"]) == 2


def test_graft_depths(log: Log, tree: dict[str, str]) -> None:
    st = log.state
    leaf = context.render_graft(
        st, ReferenceEdge(src_id=tree["b"], dst_id="x", depth="leaf"), tree["a1"]
    )
    assert "FAILED x" not in leaf.text and "b says hi" in leaf.text
    with_tool = context.render_graft(
        st,
        ReferenceEdge(src_id=tree["b"], dst_id="x", depth="leaf", include_tool_outputs=["t1"]),
        tree["a1"],
    )
    assert "FAILED x" in with_tool.text
    st.nodes[tree["b"]].summary = "SUMMARY"
    ls = context.render_graft(st, ReferenceEdge(src_id=tree["b"], dst_id="x"), tree["a1"])
    assert "SUMMARY" in ls.text
    br = context.render_graft(
        st, ReferenceEdge(src_id=tree["b"], dst_id="x", depth="branch"), tree["a1"]
    )
    assert "start" not in br.text  # the LCA (root) is shared, so it is excluded
    assert f"<node id={tree['b']}>" in br.text


def test_cannot_graft_an_ancestor(log: Log, tree: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        context.render_graft(log.state, ReferenceEdge(src_id=tree["a"], dst_id="x"), tree["a1"])


def test_cache_points(log: Log, tree: dict[str, str]) -> None:
    ctx = context.build(log.state, tree["a1"], [], "x")
    root_end = len(log.state.nodes[tree["root"]].payload) - 1
    assert ctx.cache_points == [root_end, ctx.payload_start - 1]
    assert len(ctx.cache_points) <= 3


# --- graph ---------------------------------------------------------------------


def test_lca_and_fork(log: Log, tree: dict[str, str]) -> None:
    assert graph.lca(log.state, tree["a1"], tree["b"]) == tree["root"]
    assert graph.fork_point(log.state, tree["a1"]) == tree["root"]
    assert graph.fork_point(log.state, tree["b"]) == tree["root"]


def test_trunk_heuristic_and_pin(log: Log, b: Builder, tree: dict[str, str]) -> None:
    assert graph.trunk(log.state) == tree["a1"]
    b2 = b.node(tree["b"], "b2")
    assert graph.trunk(log.state) == b2  # equal depth -> most recent
    log.append(TrunkPinned(node_id=tree["a1"]))
    assert graph.trunk(log.state) == tree["a1"]
    log.append(NodeArchived(ids=graph.subtree(log.state, tree["a"])))
    assert graph.trunk(log.state) == b2  # pin no longer live -> heuristic


def test_archive_restore_roundtrip(log: Log, tree: dict[str, str]) -> None:
    before = context.build(log.state, tree["a1"], [], "x").model_dump_json()
    ids = graph.subtree(log.state, tree["a"])
    assert ids == [tree["a"], tree["a1"]]
    log.append(NodeArchived(ids=ids))
    assert all(log.state.nodes[i].status == "archived" for i in ids)
    with pytest.raises(ValueError):
        context.build(log.state, tree["a1"], [], "x")
    assert graph.live_leaves(log.state) == [log.state.nodes[tree["b"]]]
    log.append(NodeRestored(ids=ids))
    assert context.build(log.state, tree["a1"], [], "x").model_dump_json() == before


# --- store -------------------------------------------------------------------------


def test_fold_is_pure_and_idempotent(log: Log, tree: dict[str, str]) -> None:
    s1, s2 = fold(log.events), fold(list(log.events))
    assert s1 == s2
    reloaded = Log(log.path)
    assert reloaded.state == log.state
    assert [dumps(e) for e in reloaded.events] == [dumps(e) for e in log.events]


def test_incomplete_node_is_tombstoned_on_fold(log: Log, tree: dict[str, str]) -> None:
    log.append(NodeCreated(id="crash", parent_id=tree["a1"], model="fake"))
    assert log.state.nodes["crash"].status == "tombstone"
    assert graph.trunk(log.state) == tree["a1"]
