"""The ledger (§7): prices, trunk burden, operation previews, cache-impact colouring."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kaipi import context, graph
from kaipi.model import Depth, ReferenceEdge, Usage
from kaipi.store import State

PACKAGED_PRICING = Path(__file__).resolve().parent / "pricing.toml"


def user_pricing() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "kaipi" / "pricing.toml"


def pricing_path(cwd: Path | None = None) -> Path:
    """Prices are a thing users need to correct without editing site-packages, so a project
    file wins over a user file, which wins over the copy shipped with the package."""
    # `.kaipi/pricing.toml`, not a bare `pricing.toml`: plenty of projects have one of those
    # already and it has nothing to do with model prices.
    for candidate in ((cwd or Path.cwd()) / ".kaipi" / "pricing.toml", user_pricing()):
        if candidate.is_file():
            return candidate
    return PACKAGED_PRICING


Color = Literal["green", "red"]
Estimator = Callable[[str], int]


class Price(BaseModel):
    provider: str = "anthropic"  # a key of pricing.toml [providers] or a built-in preset
    adaptive_thinking: bool = True
    context: int = 0  # context window, for the status bar; 0 means unknown
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
    providers: dict[str, Any] = Field(default_factory=dict)  # [providers.<name>] overrides

    @classmethod
    def load(cls, path: Path | None = None) -> Pricing:
        chosen = path or pricing_path()
        raw = tomllib.loads(chosen.read_text(encoding="utf-8"))
        providers = raw.get("providers", {})
        if providers and chosen not in (PACKAGED_PRICING, user_pricing()):
            # A `[providers]` table names an endpoint URL and an environment variable to send
            # to it as a credential. Honouring that from a file inside the repository being
            # worked on would let any cloned project point kaipi at its own server, collect
            # the user's API key, and then answer as the model - whose tool calls kaipi runs.
            # Prices from a project file are harmless; endpoints and secrets are not.
            print(
                f"{chosen}: ignoring its [providers] table. Endpoints and key names are only "
                "read from ~/.config/kaipi/pricing.toml or the packaged defaults."
            )
            providers = {}
        return cls(**raw.get("defaults", {}), models=raw.get("models", {}), providers=providers)

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


Kind = Literal["exploration", "graft", "interrupt"]


class Saving(BaseModel):
    """Tokens kept out of the trunk, and what that has been worth so far.

    `tokens` is per trunk turn: it is what every future turn on the trunk would re-read if
    this work had been done on the trunk instead. `cost` turns that into money actually not
    spent - tokens x the trunk model's cached-read price x the trunk turns taken since -
    so it only ever counts turns that really happened."""

    kind: Kind
    node_id: str
    tokens: int
    turns: int  # trunk turns taken since, i.e. how many times the saving has been collected
    cost: float


def savings(state: State, pricing: Pricing, estimate: Estimator = estimate_tokens) -> list[Saving]:
    t = graph.trunk(state)
    trunk_nodes = graph.lineage(state, t) if t else []
    trunk_ids = {n.id for n in trunk_nodes}
    rate = pricing.price(state.model).cache_read / 1_000_000

    def collected(seq: int) -> int:  # trunk turns that would have re-read those tokens
        return sum(1 for n in trunk_nodes if n.seq > seq)

    out: list[Saving] = []
    for n in state.nodes.values():
        tokens = 0
        kind: Kind = "exploration"
        if n.status == "aborted":
            # Everything the model produced before the user stopped it. Had it been kept,
            # the branch would carry it for good.
            kind, tokens = "interrupt", estimate("".join(str(m) for m in n.payload))
        elif n.status == "live" and n.completed and n.id not in trunk_ids:
            # An exploration: its own tokens never entered the trunk's context.
            tokens = own_tokens(state, n.id)
        if tokens > 0:
            out.append(
                Saving(
                    kind=kind,
                    node_id=n.id,
                    tokens=tokens,
                    turns=collected(n.seq),
                    cost=tokens * rate * collected(n.seq),
                )
            )
    for e in state.edges:
        # A graft carries a snapshot instead of the branch it came from. What it saved is
        # the difference between the depth chosen and inlining that whole branch.
        dst = state.nodes.get(e.dst_id)
        if dst is None or dst.status != "live":
            continue
        try:
            sizes = graft_preview(state, dst.parent_id, e, estimate)
        except ValueError:
            continue
        tokens = sizes["branch"] - sizes[e.depth]
        if tokens > 0:
            out.append(
                Saving(
                    kind="graft",
                    node_id=e.dst_id,
                    tokens=tokens,
                    turns=collected(dst.seq),
                    cost=tokens * rate * collected(dst.seq),
                )
            )
    return out


def saved_by_kind(state: State, pricing: Pricing) -> dict[Kind, tuple[int, float]]:
    """{kind: (tokens per trunk turn, money not spent so far)}."""
    out: dict[Kind, tuple[int, float]] = {
        "exploration": (0, 0.0),
        "graft": (0, 0.0),
        "interrupt": (0, 0.0),
    }
    for s in savings(state, pricing):
        tok, cost = out[s.kind]
        out[s.kind] = (tok + s.tokens, cost + s.cost)
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
    usage: Usage  # nodes + graft summaries: everything the API billed
    trunk: str | None
    trunk_burden: int
    branches: list[Branch]


def total_cost(state: State, pricing: Pricing) -> float:
    """Every dollar the session spent: the turns plus the graft summaries, which are
    charged to no node but are real money."""
    return sum(pricing.price(n.model).cost(n.usage) for n in state.nodes.values()) + sum(
        pricing.price(m).cost(u) for m, u in state.summaries
    )


def report(state: State, pricing: Pricing) -> Report:
    total = Usage()
    for n in state.nodes.values():
        total = total + n.usage
    for _, usage in state.summaries:
        total = total + usage
    cost = total_cost(state, pricing)
    t = graph.trunk(state)
    trunk_ids = {n.id for n in graph.lineage(state, t)} if t else set()
    branches: list[Branch] = []
    for leaf in state.nodes.values():
        if leaf.status in ("tombstone", "aborted") or leaf.id in trunk_ids:
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
