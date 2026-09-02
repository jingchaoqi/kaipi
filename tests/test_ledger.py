from __future__ import annotations

from kaipi import graph, ledger
from kaipi.model import NodeArchived, ReferenceEdge, Usage
from kaipi.store import Log
from tests.conftest import Builder


def test_pricing_and_cost() -> None:
    p = ledger.Pricing.load()
    fable = p.price("claude-fable-5-1")
    assert fable.cache_read == 0.25 and fable.cache_write == 12.5
    u = Usage(input_uncached=1_000_000, cache_write=0, cache_read=1_000_000, output=0)
    assert fable.cost(u) == 10.25
    assert p.price("unknown-model").cost(u) == 0.0
    assert p.price("gpt-5").provider == "openai"


def test_burden_and_own_tokens(log: Log, tree: dict[str, str]) -> None:
    st = log.state
    assert ledger.trunk_burden(st) == 300
    assert ledger.own_tokens(st, tree["a1"]) == 100
    assert ledger.own_tokens(st, tree["root"]) == 100
    assert ledger.delta(31_200, 32_000) == "trunk burden: 31.2k -> 32.0k (+0.8k)"


def test_graft_preview_orders_depths(log: Log, tree: dict[str, str]) -> None:
    e = ReferenceEdge(src_id=tree["b"], dst_id="x")
    pv = ledger.graft_preview(log.state, tree["a1"], e)
    assert pv["leaf"] <= pv["leaf+summary"]
    assert pv["leaf"] < pv["branch"] or pv["leaf"] == pv["branch"]
    with_tool = ledger.graft_preview(
        log.state, tree["a1"], e.model_copy(update={"include_tool_outputs": ["t1"]})
    )
    assert with_tool["leaf"] > pv["leaf"]


def test_cache_color_stays_green_for_subtree_ops(log: Log, tree: dict[str, str]) -> None:
    assert ledger.cache_color(log.state)[0] == "green"
    log.append(NodeArchived(ids=graph.subtree(log.state, tree["a"])))
    assert ledger.cache_color(log.state)[0] == "green"
    # a hole in a live lineage (never produced by kaipi's own ops) must be detected as red
    log.state.nodes[tree["root"]].status = "archived"
    assert ledger.cache_color(log.state)[0] == "red"


def test_report(log: Log, b: Builder, tree: dict[str, str]) -> None:
    b2 = b.node(tree["b"], "b2", tokens=400)
    log.append(NodeArchived(ids=graph.subtree(log.state, tree["b"])))
    r = ledger.report(log.state, ledger.Pricing())
    assert r.trunk == tree["a1"] and r.trunk_burden == 300
    assert r.usage.input_uncached == 100 + 200 + 300 + 250 + 400
    assert [br.leaf_id for br in r.branches] == [b2]
    assert r.branches[0].nodes == 2 and r.branches[0].status == "archived"
    assert r.branches[0].blocked_tokens == 150 + 150
