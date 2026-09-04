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
subtree is one atomic operation), `aborted` (the user stopped the turn: out of context
for good, still billed), `tombstone` (permanent; one line remains).

A node that was started and neither completed nor aborted - a crash, a kill -9 - folds to
`tombstone`: there are no half-frozen payloads.

### 1.1 Interrupting a turn

Esc in the terminal and the stop button on the canvas do the same thing. The turn ends at
the next step boundary, and the command it was running is killed with its process group,
so a 30-second test run stops now rather than in 30 seconds.

What the model produced after the user's last input - its text, its tool calls, their
output - is **discarded from context**: an `aborted` node is never assembled into another
request. The payload is kept so the user can look at what was thrown away, and both
surfaces mark the node unmistakably (`[已废弃 · 不进上下文]` in the tree, struck through
in the warning colour on the canvas).

Two things follow. The tokens it burned **are** billed - they were really spent, and a
ledger that hid them would be lying. And the cursor moves back to the node's **parent**,
so the next input opens a *sibling*: you retry from where you were, not from the wreck.

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

## 5. The seven invariants (each has a test)

1. Every node has exactly one parent.
2. Reference edges point only at earlier-created nodes (checked on log sequence, not
   on id strings).
3. A completed node's payload is frozen; grafts are snapshots.
4. The trunk only receives explicit grafts.
5. Explorations do not change the disk.
6. Context is derived deterministically from (leaf, reference edges, archived set).
7. An aborted turn is billed and never re-read: its tokens are in the ledger, its payload
   is in no future request, and the cursor sits on its parent.

## 6. Decisions the startup prompt left open

| Question | Decision | Why |
|---|---|---|
| Archiving a mid-lineage node: who becomes the children's parent? | Not allowed. Archive is always a whole subtree. | Keeps every live lineage append-only, so no operation rewrites a cached prefix or invalidates preserved-thinking blocks. `ledger.cache_color` still *computes* red/green rather than assuming green. |
| Fork point when the first sibling is created | Computed from the graph as it is when the request is sent | The first exploration pays one cache write; later siblings share from the fork. |
| Interrupted turn | Tombstone on fold | No partial payloads. |
| Exploration dirtied the tree and the user re-pins it as trunk | Allowed with a warning | §6 of the prompt says so; the ledger does not intervene. |
| Graft depth `branch` with the source on the current lineage | Rejected | Nothing to graft. |

## 7. Surfaces

The session lives in one process and is open on exactly one surface at a time: the
terminal (readline) or the canvas (a local web page served by the same process). `/canvas`
in the terminal opens the canvas and closes the prompt; `/cli` on the canvas closes the
canvas and reopens the prompt. Both surfaces call the same verbs; the canvas adds
gestures for them — drag a node onto the input to graft, onto the tray to archive.

A lock file (`.kaipi/lock.json`, pid + surface) refuses a second mutating `kaipi` in the
same directory while a surface is open.

The canvas API can run bash, so it accepts a request only when all three hold: the Host
header names loopback and the port actually bound (comparing Origin to the client's own Host
would accept a DNS-rebound name, where both say `evil.com`); Origin, if present, is the
address served; and the request carries the per-run token that appears only in the URL kaipi
opened. Together these keep out a page the user happens to have open, a page that guessed
the port, and another process on the same machine. Requests must also be `application/json`,
which forces a preflight the server never answers.

Endpoint configuration is trusted differently from prices. `.kaipi/pricing.toml` inside a
project may set prices and pick a model; its `[providers]` table is ignored with a warning,
because that table names a URL and an environment variable to send there as a credential -
a cloned repository could otherwise collect the user's API key and then answer as the model,
whose tool calls kaipi executes. Endpoints come only from `~/.config/kaipi/pricing.toml` or
the packaged defaults. Slash commands that
would prompt on the terminal (`/rewind` without a mode) are answered with the information
instead; `/explore` on the canvas runs as a normal streamed turn. Read-only commands (`tree`, `ledger`, `trunk`,
`sessions`) are always allowed.

Trunk pinning follows two rules so the trunk never silently moves under the user:
leaving the trunk leaf with `go` pins it (even if a stale pin points elsewhere); extending
a pinned leaf moves the pin to the new node. `/explore` (or "作为探索" on the canvas) opens an exploration from the trunk
leaf itself by pinning first.

## 8. Code checkpoints and rewind

Every turn snapshots the working tree before and after it runs (a git tree object written
through kaipi's private index, so the user's index, stash and branches are untouched).
The node records the tree at the end of the turn and the list of paths the turn changed.
Trees are pinned under `refs/kaipi/<node id>` so `git gc` keeps them.

`rewind <node>` offers the three choices Claude Code offers: conversation only (the same
as `go`), code only, or both. A code rewind restores **only the files touched by turns
after that node** to their content at that node; it never touches files the agent did not
change, so manual edits elsewhere survive. A manual edit to a file the agent also changed
is overwritten (file-level, not hunk-level). The pre-rewind tree is pinned at
`refs/kaipi/undo`.

The exploration guard uses the same snapshot plus HEAD: dirty means the tree hash or
the commit HEAD points at changed, so a `git commit` on an exploration is caught too.
Paths are repo-root relative whatever subdirectory kaipi runs in. A missing snapshot
object makes `rewind` refuse rather than delete. Non-git directories get no snapshots and
no code rewind. `rewind` in `both` or `conversation` mode requires a live target; `code`
mode also accepts archived nodes.

## 9. Cache breakpoints

Anthropic allows four `cache_control` breakpoints per request. kaipi uses exactly:

1. end of the system prompt (caches tools + system),
2. the last message of the fork point (nearest ancestor with ≥ 2 live children),
3. the last message of the current lineage,
4. the last message of the request (moves with every tool step).

Sibling explorations therefore share the prefix up to the fork. Breakpoints are added
when the request is built and never stored in payloads.

These four positions are what makes sibling explorations share a prefix: measured against
the mock endpoint, two explorations from the same fork read byte-identical amounts, and the
first request of each trunk turn reads the previous turn back through the 20-position
lookback window rather than rewriting it.

Preserved thinking (Claude Fable 5.1) binds thinking blocks to the prefix that produced
them. kaipi never edits history, but sends `prefix_mismatch_behavior: drop_block` so
that if a prefix ever changes the API degrades instead of failing, and the count of
dropped blocks is recorded on the node and shown in the ledger.
