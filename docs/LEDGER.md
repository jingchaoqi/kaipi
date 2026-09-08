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

## Graft summaries

A `leaf+summary` graft makes one extra request to the cheap model (`summary_model` in
`pricing.toml`). That spend belongs to no node, so it is recorded on the
`summary_generated` event and added to the session totals separately, priced at the cheap
model's rate. `scripts/smoke.py` checks that the ledger totals equal the sum of what the
API reported, which is what caught this being missing.

## Savings: what the shape of the session is worth

`kaipi ledger` and both status bars report savings from the three things kaipi does that a
linear chat cannot. Each is measured the same way: **tokens the trunk does not re-read on
every turn**, and the money that has therefore not been spent so far.

| Source | Tokens counted | Why they would otherwise be in the trunk |
|---|---|---|
| exploration | the branch's own tokens | the same work done on the trunk would sit in every later request |
| graft | `branch` size − the depth actually used | a graft carries a snapshot of the conclusion, not the branch it came from |
| interrupt | the discarded turn's payload | a turn the user stopped would otherwise stay in the branch for good |

The money figure is deliberately conservative: `tokens x the trunk model's cached-read
price x the number of trunk turns taken since the saving existed`. It only ever counts
turns that really happened, so it is money not spent rather than a projection. A saving
that appeared on the last turn therefore reads `$0.0000` - it has not been collected yet.

Two things this is not. It is not a claim about what another tool would have cost: it
compares against *the same work on the trunk*, which is what a linear chat would do with
it. And an aborted turn's own spend is still counted as **spend** in the totals - the
saving is the re-reading that never happens, not a refund.

## Turns that did not finish

A turn stopped by the user and a turn ended by the provider are billed the same way: the
node is `aborted`, and whatever it burned before it ended is in the totals. Only the
re-reading is saved, never the spend - see the `interrupt` row above. A node that was
created and then neither completed nor aborted (a crash, a kill -9) is a tombstone and
costs nothing, because nothing was ever recorded for it.

## What is not in the ledger

Code snapshots and rewind cost no tokens and are not reported. Dropped thinking blocks
(`input_transformations`) are the only non-token quantity shown, because they are the
symptom of a prefix edit.

## Caveats

- Prompts shorter than the model's minimum cacheable prefix (512–4096 tokens depending
  on model) are silently not cached. Early in a session `cache_read` is 0; not a bug.
- OpenAI-compatible endpoints report only what they report (`cached_tokens` or
  `prompt_cache_hit_tokens`); there is no write premium, so `cache_write` is 0.
- Dropped thinking blocks (`input_transformations` from the API) are counted per node
  and totalled in `kaipi ledger`. A non-zero total means a prefix was edited.
