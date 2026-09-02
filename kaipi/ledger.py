"""The ledger (§7): prices, trunk burden, operation previews, cache-impact colouring."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kaipi import context, graph
from kaipi.model import Depth, ReferenceEdge, Usage
from kaipi.store import State

DEFAULT_PRICING = Path(__file__).resolve().parent.parent / "pricing.toml"
Color = Literal["green", "red"]
Estimator = Callable[[str], int]


class Price(BaseModel):
    provider: Literal["anthropic", "openai"] = "anthropic"
    adaptive_thinking: bool = True
    input: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0
    output: float = 0.0

    def cost(self, u: Usage) -> float:
        return (
            u.input_uncached * self.input
            + u.cache_write * self.cache_write
            + u.cache_read * self.cache_read
            + u.output * self.output
        ) / 1_000_000


class Pricing(BaseModel):
    model: str = "claude-opus-5"
    summary_model: str = "claude-haiku-4-5"
    models: dict[str, Price] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_PRICING) -> Pricing:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(**raw.get("defaults", {}), models=raw.get("models", {}))

    def price(self, model: str) -> Price:
        """Unknown models cost 0 so the ledger still adds up in tokens; the CLI warns."""
        return self.models.get(model, Price())


def estimate_tokens(text: str) -> int:
    """Crude fallback estimator; providers may supply a real count_tokens."""
    return max(1, len(text) // 4)


# --- burden ------------------------------------------------------------------


def burden(state: State, leaf_id: str | None) -> int:
    """Tokens every new trunk turn re-pays: the context size measured at the leaf."""
    return state.nodes[leaf_id].context_tokens if leaf_id else 0


def trunk_burden(state: State) -> int:
    return burden(state, graph.trunk(state))


def own_tokens(state: State, node_id: str) -> int:
    """What this node alone added to the context (API-measured, so >= 0 by clamping)."""
    n = state.nodes[node_id]
    parent = state.nodes[n.parent_id].context_tokens if n.parent_id else 0
    return max(0, n.context_tokens - parent)


def fmt(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k"


def delta(before: int, after: int) -> str:
    sign = "+" if after >= before else "-"
    return f"trunk burden: {fmt(before)} -> {fmt(after)} ({sign}{fmt(abs(after - before))})"


# --- previews --------------------------------------------------------------------


def graft_preview(
    state: State, leaf_id: str | None, edge: ReferenceEdge, estimate: Estimator = estimate_tokens
) -> dict[Depth, int]:
    out: dict[Depth, int] = {}
    for d in ("leaf", "leaf+summary", "branch"):
        block = context.render_graft(state, edge.model_copy(update={"depth": d}), leaf_id)
        out[d] = estimate(block.text)
    return out


def cache_color(state: State) -> tuple[Color, str]:
    """Red iff some live leaf's lineage has a hole (a non-live ancestor): that prefix will be
    rewritten and, on models with preserved thinking, later thinking blocks dropped.
    Every operation kaipi offers is subtree-atomic, so this stays green; it is computed rather
    than assumed so a future mid-lineage archive cannot silently lie."""
    for leaf in graph.live_leaves(state):
        if any(n.status != "live" for n in graph.lineage(state, leaf.id)):
            return "red", f"prefix of {leaf.id} changes: cache rewrite + thinking blocks dropped"
    return "green", "append-only: cached prefixes untouched"


# --- report --------------------------------------------------------------------------


class Branch(BaseModel):
    leaf_id: str
    status: str
    nodes: int
    one_time_cost: float
    blocked_tokens: int  # tokens the trunk would otherwise re-pay every turn


class Report(BaseModel):
    total_cost: float
    usage: Usage
    trunk: str | None
    trunk_burden: int
    branches: list[Branch]


def report(state: State, pricing: Pricing) -> Report:
    total = Usage()
    cost = 0.0
    for n in state.nodes.values():
        total = total + n.usage
        cost += pricing.price(n.model).cost(n.usage)
    t = graph.trunk(state)
    trunk_ids = {n.id for n in graph.lineage(state, t)} if t else set()
    branches: list[Branch] = []
    for leaf in state.nodes.values():
        if leaf.status == "tombstone" or leaf.id in trunk_ids:
            continue
        if graph.children(state, leaf.id, live_only=False):
            continue
        off = [n for n in graph.lineage(state, leaf.id) if n.id not in trunk_ids]
        branches.append(
            Branch(
                leaf_id=leaf.id,
                status=leaf.status,
                nodes=len(off),
                one_time_cost=sum(pricing.price(n.model).cost(n.usage) for n in off),
                blocked_tokens=sum(own_tokens(state, n.id) for n in off),
            )
        )
    return Report(
        total_cost=cost, usage=total, trunk=t, trunk_burden=burden(state, t), branches=branches
    )
