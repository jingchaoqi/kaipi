# kaipi concepts

kaipi (开辟) is a minimal, token-frugal coding agent. Its one idea: organise the
human–agent exploration as a DAG so the **trunk** — the context every future turn
re-pays — stays as small as possible. Explorations cost once; trunk context costs
compound interest.

This file is the constitution. Every semantic change lands here first.

## 1. Node

A node is one complete turn: one user input plus everything the model did until it
stopped, **including every tool call and its output**. Nodes are the smallest unit of
context. Tool calls are never nodes of their own.

Once the turn ends, the node's `payload` is frozen. Anything that looks like a change is
a new node or a status change.

Status: `live` (part of context), `archived` (out of context, reversible; archiving a
subtree is one atomic operation), `tombstone` (permanent; one line remains).

A node that was started but never completed (crash, Ctrl-C) folds to `tombstone`:
there are no half-frozen payloads.

## 2. Edges

**Lineage**: every node has exactly one `parent_id` (roots excepted). This is the tree
that defines default context.

**Reference (graft)**: created explicitly by the user, from any existing node to the
node *being created*. A reference can only point at an earlier node, so the graph is
acyclic by construction. Depth:

- `leaf`: the source's user input + final answer; tool outputs omitted.
- `leaf+summary` (default): `leaf` plus a summary of the path from the common ancestor
  to the source, generated lazily by a cheap model.
- `branch`: every node after the common ancestor, inlined; tool outputs still omitted.

`--with-tool <tool_use id>` includes specific tool outputs (a failing test log, say).
That is the only exception to "grafts carry conclusions, not transcripts".

Grafting a node that is already in the current lineage is rejected.

## 3. Trunk and explorations

There is no branch object. A branch is the path from a root to a leaf. The trunk is a
pinned leaf; without a pin it is the deepest live leaf, most recently created on ties.
The user can re-pin at any time.

The trunk is the only path allowed to write to disk. Every other path is an
exploration whose invariant is: **the git working tree is identical on entry and exit**.

**The trunk only receives what is explicitly dragged in.** Abandoning a branch injects
nothing into the trunk. If a branch produced something worth keeping, graft its leaf.
Explicit beats implicit.

## 4. Context assembly (deterministic)

For a new node under leaf L:

```
context = system_prompt
        + payloads of all live nodes on lineage(L), in lineage order
        + ONE user message holding every graft block, in the order the user gave
        + the new user input (prefixed by the read-only notice on explorations)
```

Graft blocks are appended **after the lineage and before the new input, never in the
middle** — the lineage prefix stays byte-identical, so cached prefixes survive. All
grafts share one message so they occupy one cache position.

Graft blocks are snapshotted into the new node's payload at creation. Archiving or
changing the source later does not touch them. A graft is paid for once; afterwards it
is prefix and descendants inherit it through the cache.

Context is a pure function of (leaf, reference edges, archived set). The only
session-level input is the system prompt, which is frozen at `session_started`
(`AGENTS.md` is snapshotted into it) because editing it invalidates every cache below.

The exploration read-only notice is a text block in the new user message, **not** a
system-prompt suffix: a different system prompt would give explorations a different
cache namespace from the trunk they fork from.

## 5. The six invariants (each has a test)

1. Every node has exactly one parent.
2. Reference edges point only at earlier-created nodes (checked on log sequence, not
   on id strings).
3. A completed node's payload is frozen; grafts are snapshots.
4. The trunk only receives explicit grafts.
5. Explorations do not change the disk.
6. Context is derived deterministically from (leaf, reference edges, archived set).

## 6. Decisions the startup prompt left open

| Question | Decision | Why |
|---|---|---|
| Archiving a mid-lineage node: who becomes the children's parent? | Not allowed. Archive is always a whole subtree. | Keeps every live lineage append-only, so no operation rewrites a cached prefix or invalidates preserved-thinking blocks. `ledger.cache_color` still *computes* red/green rather than assuming green. |
| Fork point when the first sibling is created | Computed from the graph as it is when the request is sent | The first exploration pays one cache write; later siblings share from the fork. |
| Interrupted turn | Tombstone on fold | No partial payloads. |
| Exploration dirtied the tree and the user re-pins it as trunk | Allowed with a warning | §6 of the prompt says so; the ledger does not intervene. |
| Graft depth `branch` with the source on the current lineage | Rejected | Nothing to graft. |

## 7. Cache breakpoints

Anthropic allows four `cache_control` breakpoints per request. kaipi uses exactly:

1. end of the system prompt (caches tools + system),
2. the last message of the fork point (nearest ancestor with ≥ 2 live children),
3. the last message of the current lineage,
4. the last message of the request (moves with every tool step).

Sibling explorations therefore share the prefix up to the fork. Breakpoints are added
when the request is built and never stored in payloads.

Preserved thinking (Claude Fable 5.1) binds thinking blocks to the prefix that produced
them. kaipi never edits history, but sends `prefix_mismatch_behavior: drop_block` so
that if a prefix ever changes the API degrades instead of failing, and the count of
dropped blocks is recorded on the node and shown in the ledger.
