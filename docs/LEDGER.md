# The ledger: what the numbers mean

Every request records four token counts from the API's own `usage`: `input_uncached`,
`cache_write`, `cache_read`, `output`. A node's `usage` is the sum over every request in
its turn. Cost is `usage × pricing.toml` prices for the node's model. Unknown models
cost 0 and the CLI warns.

`pricing.toml` stores **four explicit prices per model** rather than multipliers:
cache reads are 0.1× base input on most models but 0.025× on Claude Fable 5.1, and
1-hour-TTL cache writes are 2× rather than 1.25×. kaipi uses the 5-minute TTL.

## Trunk burden

The context size measured by the API on the last request of the trunk leaf's turn
(`context_tokens` = uncached + cache write + cache read + that request's output). It is
what every future trunk turn re-reads — the compound-interest number. Printed after
every turn.

A node's **own tokens** = its `context_tokens` − its parent's, clamped at 0. That is
what the tree view shows per node.

## Operation previews

Any operation that can change the trunk burden (graft, archive, restore, pin) prints
`trunk burden: before -> after (delta)` first. Grafts also print the estimated size of
all three depths; on Anthropic the estimate uses `count_tokens`, elsewhere `len/4`.

## Cache impact colouring

- **green** — append-only: new node, graft, continuing on a leaf, archiving a subtree,
  restoring, re-pinning. No cached prefix changes.
- **red** — a live leaf's lineage now has a hole (an archived or tombstoned ancestor).
  Its prefix would be rewritten and, on models with preserved thinking, thinking blocks
  after the hole dropped. kaipi's own operations are all subtree-atomic and therefore
  never red; the colour is computed from the graph, not assumed.

## Explorations

For every leaf not on the trunk (live or archived), the ledger reports:

- **one-time cost** — the cost of the nodes on that branch that are not on the trunk.
- **tokens kept out of the trunk** — the sum of those nodes' own tokens. Had the same
  work been done on the trunk, this many tokens would sit in the trunk burden and be
  re-paid on every later turn.

Cache reads on the first request of an exploration will typically cover the prefix up
to the fork point; the CLI prints `cache read x/y` after each turn so this is visible.

## Caveats

- Prompts shorter than the model's minimum cacheable prefix (512–4096 tokens depending
  on model) are silently not cached. Early in a session `cache_read` is 0; not a bug.
- OpenAI-compatible endpoints report only what they report (`cached_tokens` or
  `prompt_cache_hit_tokens`); there is no write premium, so `cache_write` is 0.
- Dropped thinking blocks (`input_transformations` from the API) are counted per node
  and totalled in `kaipi ledger`. A non-zero total means a prefix was edited.
