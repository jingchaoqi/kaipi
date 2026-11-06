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

### 1.0 User attachments

Explicitly pasted/selected attachments are draft UI state until submitted. The input
message then contains immutable content snapshots: base64 image/document blocks for
normalized raster images and original PDF documents; tagged text blocks for UTF-8
files and extracted document contents. Images are identified from bytes using Pillow
and HEIF decoding, not extension checks; supported orientation/colour transformations,
resizing and all bounded frames are handled before snapshots are frozen. Conversion
metadata retains the original filename and describes any transformation. Local document
converters handle other known content types; raw ZIP recursion and URL/cloud/plugin
conversion are disabled. Workbooks retain formulas as source text. Text snapshots receive credential redaction.
All format-specific logic lives in the `attachments` package, behind one replaceable
callable: `parse(name, raw_bytes) -> (content_blocks, summary)`. The default implementation
loads decoder libraries lazily. `Input` captures the validated blocks before entering
the agent; context assembly only copies those snapshots and never invokes a parser.
There is no converter registry, discovery mechanism, or plugin lifecycle in the agent.
Replaying a node uses its payload, never the original file or clipboard. Question
labels omit attachment bodies; the visible question retains numbered attachment markers. Grafts
carry question/answer text and summaries as before, without copying binary attachments.

The terminal recognizes complete pasted file selections (quoted/escaped paths or
local file:// URLs), and Ctrl-V explicitly reads the local clipboard on macOS/Linux.
Ordinary sentences mentioning paths are not implicitly read. Draft markers can be
removed; queued submissions keep their own attachment snapshots. Enter during an
in-flight clipboard read waits for it; successive pastes accumulate, including while a
previous read is pending. Image and file markers use separate counters per submitted
draft (`[image1]`, `[image2]`, `[file1]`); removing a marker does not renumber the others.
A failed read keeps the draft. Command submissions
with attachments are rejected except `/explore`, which keeps the same attachments.
The Canvas supports clipboard files, file selection and file drop, with removable
chips and expanded transcript previews. Invalid/unsupported files are rejected before
a turn starts; failed requests retain the browser draft.

The four protocol adapters translate image/PDF blocks without silently dropping them.
Actual media support depends on the selected model/gateway. Plain text request shapes
stay unchanged. Drafts allow eight originals, 20 MiB per original and 40 MiB total;
normalized image/PDF payloads allow 5 MiB per block and 10 MiB total. PDFs remain
bounded at 5 MiB; images allow eight frames per file, sixteen per message, 64 million
pixels per decoded frame and a 4096-pixel long edge in output. Extracted text is limited
to 128 KiB per file. The local JSON request cap is 56 MiB. Document archives are bounded
before extraction; formulas and macros are never executed. A token-authenticated local
`/api/attachments` endpoint validates/converts drafts and returns summaries and marker kinds
without creating a node; submission validates again and freezes the same representation.

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

### 1.2 A turn the provider ended

A rate limit is not a failure: an entry-tier endpoint can allow three requests a minute,
and one turn with six commands is seven requests in as many seconds. The agent waits it
out - the vendor's `retry-after` when it sends one, exponential backoff otherwise, up to
five times and never more than a minute at a stretch - announcing each wait, because the
difference between slow and hung is the only thing the user cannot see. Esc still works
during a wait.

Anything else the provider does - a 401, a dead network, an expired key - ends the turn
the way a stop does: the node becomes `aborted`, so the tokens it already burned stay on
the bill, the cursor moves back to the parent, and the surface prints one sentence. A
provider must never be able to take a session down with it.

## 2. Edges

**Lineage**: every node has exactly one `parent_id` (roots excepted). This is the tree
that defines default context.

**Reference (graft)**: created explicitly by the user, from any existing node to the
node *being created*. A reference can only point at an earlier node, so the graph is
acyclic by construction. Depth:

- `leaf`: the source's user input + final answer; tool outputs omitted.
- `leaf+summary` (default): `leaf` plus a summary of the path from the common ancestor
  to the source, generated lazily by a cheap model - the session's own unless the packaged
  `summary_model` belongs to the same vendor (see LEDGER.md).
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

### 3.1 Changing which path is the trunk

`trunk pin <id>` moves the trunk to another path. Nothing is deleted and no payload is
touched: the old trunk becomes an ordinary off-trunk branch, still live, still reachable,
and its tokens start counting as savings rather than as burden. The operation is a pure
accounting change, which is why the command prints `(no prefix changes)` - the assembled
prefixes are byte-identical, so cached prefixes stay valid. In particular the fork point
keeps its breakpoint: a branch promoted to trunk goes on reading the same cached prefix
it was already sharing with its old sibling.

The cursor is separate from the trunk - one is where you are, the other is what you are
building on - but pinning moves the cursor onto the pinned node. A turn is an exploration
when the cursor is not on the trunk leaf, so a pin that left the cursor behind would make
the very next turn a read-only exploration of the branch just abandoned. When the pinned
node is not live the pin is stale, the heuristic trunk stands, and the cursor stays put.

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

`raw` prints this assembly for the turn that has not happened yet - the same
`context.build` the agent calls, with a placeholder where the user's input will go - so
what a graft really carries can be read rather than inferred. It sends nothing, so a
`leaf+summary` graft with no summary yet shows `(no summary available)` there.

Context is a pure function of (leaf, reference edges, archived set). The only
session-level input is the system prompt, which is frozen at `session_started`
(`AGENTS.md` is snapshotted into it) because editing it invalidates every cache below.

The model is not session-level: each node records the model it ran on, and the next node
runs on whatever `/model` currently names. `session_started` keeps the model the session
began with only as a record. Switching models mid-lineage rewrites the cache (caches are
model-scoped) and, with preserved thinking, drops the earlier model's thinking blocks;
kaipi allows it and lets the ledger show the cost rather than forbidding it.

The exploration read-only notice is a text block in the new user message, **not** a
system-prompt suffix: a different system prompt would give explorations a different
cache namespace from the trunk they fork from.

That notice is frozen into the payload of every turn written under it, and `trunk pin`
changes which branch may write without touching a byte of any payload (§3.1). So a branch
promoted to the trunk still has "this is read-only" in its own history, and the model obeys
it - it refused to edit files on what was by then the trunk. Editing history is not an
option (invariant 3, and it would rewrite the cache), so the correction is **appended** to
the next input, where the newest notice wins: "this branch is the trunk now, the earlier
read-only notice no longer applies". The prefix is byte-identical, so the cache survives.

Which notice a turn carries is therefore a function of two things - what the lineage last
said, and what is true now - and a turn pays for one only when they disagree: an
exploration repeats the read-only notice, a promoted branch says the release once and its
descendants inherit it, and a trunk that was never read-only carries nothing. The reverse
case needs no special handling: an old trunk that became an ordinary branch gets the
read-only notice on its next turn like any other exploration.

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
terminal or the canvas (a local web page served by the same process). The terminal keeps a
fixed area at the bottom - the input line with the status bar under it - and scrolls the
agent's output above it; while a turn runs the area stays, the bar says so, Esc stops the
turn and a line typed meanwhile is queued as the next input (`kaipi/tui.py`, on
prompt_toolkit; without a tty a plain line loop takes over). `/canvas`
in the terminal opens the canvas and closes the prompt; `/cli` on the canvas closes the
canvas and reopens the prompt. Both surfaces call the same verbs; the canvas adds
gestures for them — drag a node onto the input to graft, onto the tray to archive.

Slash completion appears above the input, with up to ten visible rows and scrolling.
Starting `kaipi` (including `-c`/`-r`) or `kaipi canvas` clears the terminal display and
scrollback before onboarding or session output. Non-interactive subcommands and piped
output are not cleared.
At the idle prompt, Ctrl-C first asks for confirmation; a second press within three
seconds exits. Editing the input cancels that confirmation. During a turn Ctrl-C still
stops the turn. After saving and releasing the lock, normal session exits clear the
terminal display and scrollback, then show a local summary: session title, working
directory, total node count (including archived/aborted nodes), cumulative cost and
`kaipi -r <full-session-id>` to resume. No model is called. Piped output is not cleared;
an empty discarded session instead prints `kaipi` to start again.

UI copy lives in a small independent translation module and a bundled Chinese/English
catalog. On a fresh installation, the first greeting and language picker are bilingual;
the choice is saved before provider/model setup, which uses that language immediately.
After successful setup, the terminal display and scrollback are cleared before entering
the session; cancelled or failed setup and piped output are not cleared.
Existing configured installations without a language preference keep Chinese. `/config`
can switch the language with either horizontal arrow; changes save immediately and
apply to subsequent CLI and Canvas rendering. No model request is made for translation.
Commands, model IDs, paths, node IDs, original errors from external services, user content
and tool output retain their original text. UI language never alters system prompts,
request assembly, stored payloads, attachment content or session titles. Chinese UI uses
consistent session/trunk/cost terminology; estimated savings remain labelled estimates.
Status phases keep a centered fixed-width slot: eight terminal cells in Chinese and the
longest translated phase in English. The catalog is checked for missing entries and
matching interpolation parameters. Canvas expands only explicitly marked static UI copy
with context-appropriate escaping; conversation content is never passed to translation.

`/statusline` (also `kaipi statusline`) selects which bottom-line elements are visible:
provider, model, session title, working directory, context usage, accumulated cost,
estimated token savings and estimated cost savings. All default to visible. Up/Down
selects an item; Left/Right cycles backward/forward through its options in a local draft.
Directory options are full (existing path display), compact (last path component only)
and off. Context options are full (existing usage display), percent (the same usage
divided by the selected model's window, with one decimal place) and off. An unknown
window is shown as unknown, never as zero percent. Other items cycle between on and off.
Legacy boolean settings retain their meaning: true becomes full/on, false becomes off.
Esc atomically saves these preferences in the user settings file and returns; Ctrl-C/Ctrl-D cancels the
draft. A failed save keeps the panel open. Returning refreshes the bar immediately,
without altering ledger calculations, `/status` details or the activity/elapsed-time row.
The line keeps its existing space even when all elements are hidden to avoid layout jumps.

`/status` (also `kaipi status`) is a local, read-only overview shared by CLI and Canvas:
version, selected next-turn model/provider, session, working directory, cursor and next-turn
mode, pending graft count, cumulative tokens/cache reads and estimated session cost.
Context usage is the current cursor node's last recorded request, compared with the
selected model's window from pricing; it is not a next-request token estimate and excludes
pending grafts. Empty sessions and unknown windows are labelled explicitly. Unpriced
historical usage is marked in the cost line. It neither creates a turn nor calls a model.

`/inspect-context` serializes only the next request's `system` and `messages` as JSON text
in a `.md` file, using the same `context.build` as the agent. All message fields and blocks
(including tools and attachments) are preserved; the next question is a placeholder.
There are no added headings, annotations or metadata. Provider wire-format conversion
happens later. No model is called and no session state is mutated.
The export goes to the session workspace root as
`YYYYMMDDHHMMSS-<session-title>-<node-label>-context.md` (local time); unsafe filename
characters are replaced, long titles shortened, and same-second collisions gain `-2`,
`-3`, etc. before `.md` without overwriting files. The command prints the absolute path.

In the terminal's completion menu, Enter submits the selected slash command, or the
first matching command when none is selected (so `/tr` submits `/tree`). An exact
command is kept as typed. After a node-taking command and a space, Enter accepts
the highlighted node argument and submits the complete command; Up/Down selects
another node without requiring Tab. Session arguments use the same behavior. Tab still completes
without submitting, and the arrow keys let the user choose another candidate.
The first match is highlighted by default; Down selects the second, then the third,
and Up moves back. Arrow navigation keeps the typed prefix unchanged. Completion is
recomputed after every edit, including Backspace and deletion. A bare `/` offers
all slash commands with Help first and highlighted. Enter submits `/help`; Up/Down
selects another command before Enter submits it. Once a prefix is entered, the
existing command order and prefix matching apply. Deleting back to `/` restores
the full menu with Help selected.

During a terminal turn, a comet and a text shimmer animate at a target 60 FPS in a
dedicated activity row. Its phase follows transient hooks (waiting for the model,
executing a command, waiting to retry) and the stop flag; these are UI state, never
session events. The ledger row refreshes separately, and the animation timer is off
at rest. Phase labels are centered in a fixed width of four Chinese characters (eight
terminal cells), so phase changes do not shift the comet, elapsed time or separators.
Questions are echoed as full-width background panels with a blank row of
padding above and below; slash commands keep their compact echo. Wrapped questions
use terminal cell widths, including CJK characters.
After a turn, the activity row stays visible with its final elapsed time and outcome
(completed, stopped, or failed) and a waiting-for-input hint. The comet, status text,
elapsed time and hint settle into a uniform dim style, with no frozen highlight.
The colors ease from the last rendered frame into gray over 0.5 seconds at a target
60 FPS; the comet's position and elapsed time stop immediately. Refreshing then stops
until the next turn. Completion does not
collapse the toolbar by removing this row.
Normal completion also keeps the current input application alive, including any
unsent draft. Output is batched and drained on the UI loop before the idle state is
drawn, rather than exiting, erasing, and recreating the prompt as the answer arrives.
The animation task ends after the fade; the idle prompt needs no polling. Queued
submitted inputs and turns requiring an error or worktree confirmation return control
to the terminal loop as before.
The activity row is reserved from the first prompt, and the input prefix has a fixed
minimum width so changing `root` to a node label does not shift a draft's cursor.
Batched output uses synchronized terminal updates where supported, grouping the
output and prompt redraw into one visible update.

The tty transcript shows each command in two bounded lines: the model's preceding
explanation (or an action label based on the command when no explanation was given),
then the command. Both lines collapse newlines and truncate to at most 120 terminal
cells, or the current width minus one if narrower. Tool stdout/stderr stays in the
node payload and can be expanded on the canvas, rather than scrolling in the tty.
Text preceding a command is held for its explanation; text left when a turn ends is
rendered as Markdown through Rich, including emphasis, headings, lists, tables and
code blocks. Rendering changes neither payloads nor requests. The plain surface
keeps its text output for pipes and scripts.
Terminal code blocks use an opaque dark syntax background. Tool rows and answer
blocks share the question panel’s two-cell left inset. The live input follows its
node label with a single space; elapsed seconds render with three decimal places
at the existing 60 FPS animation cadence and freeze at completion.
One blank line separates the tool-call section from the final answer body.
The intent and command rows share a subtle background extending to the terminal
width minus one; styling resets before the answer, and content still truncates at 120 cells.
The per-turn usage summary has one blank line above and below it, separating the
answer from the summary and the next input area's rule.

Terminal lists align columns using the widest displayed cell in the current list,
ignoring ANSI styling and counting Chinese characters at their terminal width.
Settings recalculate the layout immediately when the interface language changes.

Interactive slash commands do not echo into the conversation. Query commands (`/help`,
`/status`, `/version`, `/ledger`, `/raw`, and bare `/trunk`) use a scrollable temporary
screen, closed with Esc/Ctrl-C. Menus, configuration prompts and rewind previews also
use the terminal's alternate screen: returning or cancelling restores the conversation
and its scrollback, with no command or prompt residue. Successful actions retain only a
compact result; errors remain visible, and `/inspect-context` keeps the exported path.
`/update` shows progress temporarily and waits for return when no installation starts.
`/explore`, `/new`, surface switching and session exit retain their own lifecycle.
Plain/piped commands continue to print ordinary output without alternate-screen controls.

On a TTY, `/tree` and `/go` without arguments open one shared node navigator.
Tree connectors, node numbers, questions, current position and trunk labels are the
primary rows. Token counts, cost, graft references and a question preview are shown
for the highlighted node below the tree. Only live nodes are offered for jumping;
archived and aborted nodes remain visible in static `kaipi tree` output.
Up/Down follows tree row order; Left selects the parent; Right selects the remembered
child, otherwise a trunk child, otherwise the newest child. Navigation clamps at
boundaries and changes no session state. Enter applies the existing `go` operation,
Esc/Ctrl-C cancels. The initial selection is the current node, kept visible in a
scrolling viewport of at most ten rows. Historical destinations are labelled as
explorations; choosing one moves the conversation cursor, leaving files as they are.
`kaipi tree` and the Canvas tree API remain static. In plain terminal mode `/tree`
prints the tree and `/go` without arguments prints it and asks for a target.

`/rewind` uses the same navigator, including non-live nodes for code-only rewinds.
Enter selects a target followed by the existing file preview and rewind-mode prompt;
it does not jump immediately. Explicit arguments still work. Plain terminals print
the tree and ask for a node instead. An empty session has no selectable target.

A lock file (`.kaipi/lock.json`, pid + surface) refuses a second mutating `kaipi` in the
same directory while a surface is open.

Entering an existing CLI session (`-r`, `-c`, `/resume`, or returning from Canvas)
replays the stored root-to-cursor conversation before showing the input prompt.
Questions, compact tool calls and Markdown answers use the normal terminal rendering;
other branches, injected context blocks, thinking and attachment bodies are omitted.
Replay is display-only: it makes no model calls, executes no tools and appends no events
or usage. Switching sessions replaces the previous transcript instead of mixing them.

Every `kaipi` is a fresh conversation. An old one is picked up on purpose - `-c` for the
latest, `-r <id>` for a given one, `/resume` inside a session - never by accident. A
conversation can be named (`/rename`); the name is an event in its own log, so it folds
like everything else and travels with the file.

Every release must support direct, lossless upgrades from all earlier official releases;
downgrades are not supported. Preserve saved history, attachment bytes, usage, configuration,
cursor and Git snapshot references. Reading never rewrites old logs; continuing only appends.
If a format change needs conversion, handle it at the storage boundary, retain the original
and test the old format. Unknown events must not be silently skipped. Price-derived estimates
still use the selected pricing table; they are not frozen billing records.

`version` / `/version` read installed package metadata locally. `update` / `/update`
query the public GitHub latest-release endpoint and show both versions and the release URL.
The tty starts a daemon check, cached for 24 hours under the user cache directory, with
one notice per release; failures stay silent. `/config` (also `kaipi config`) opens a
keyboard settings panel: Up/Down selects a row, Left turns it off, Right turns it on, and
each change is saved atomically to `~/.config/kaipi/settings.json` without Enter. Esc
returns, retaining changes already made. Automatic checks default to enabled;
`KAIPI_NO_UPDATE_CHECK=1` supplies a disabled default until the user explicitly saves a
choice in the panel. A saved choice takes precedence so the visible setting is effective
immediately. Turning it on starts a background check with the same 24-hour cache;
turning it off suppresses future checks and pending notifications. Concurrent automatic
checks are coalesced. Explicit updates always check again regardless of this setting.
No project data or model credentials
are included in these requests. Only newer stable releases can be installed automatically.
The updater accepts only an identifiable uv tool installation; other installations get
manual instructions. Download and SHA-256 verification finish before leaving the session.
Queued messages or attachment/text drafts prevent the terminal handoff. Once ready, save
the cursor, release the surface lock, then replace the process with a stdlib-only installer.
It targets the same uv tool directory and entrypoint and resumes the exact session id in
the original working directory. An empty session is retained during this handoff. Failed
installation prints a retry command; history and configuration are never rewritten by
the updater. The verified wheel stays cached because uv records it as its install source.
Canvas users return with `/cli` before updating.

After the first successful root turn, an unnamed conversation gets a short title from
an independent model request using only the opening question's first 4,000 characters.
It uses the same-provider summary model (or the active model), with no project prompt,
history, attachment contents or tool execution. The answer is displayed before this
request; the activity line says that a title is being generated. Failure keeps the
opening-question preview and never fails the completed turn. The naming response must
be a JSON object containing only a nonempty, single-line `title` of at most 40 characters;
ordinary replies or malformed output are not saved as names. Previously saved and
explicitly renamed titles remain unchanged. An explicit name is never
overwritten. The title and its model/usage are recorded in `session_title_generated`;
even an empty/refused response is billed. This metadata never enters model context or
adds a node. `/rename` can replace it. Old logs remain readable without title events.
The generator lives in `titles.py`, separate from graft summarization. Its boundary is
plain opening text plus a provider in, title/usage metadata out: it receives no Session,
Log, State or context builder and cannot persist messages. Only the caller appends the
metadata event. Naming instructions, title responses and title metadata are excluded
from trunk/branch history, all graft depths, graft summaries and subsequent model input.

In the terminal, bare `/resume` opens an inline menu of this directory's conversations,
newest first. The current conversation is selected initially; Up/Down moves a highlighted
row, Enter resumes it, and Esc cancels without changes. The menu scrolls within ten rows,
retains title, short ID, time and turn count, and truncates long rows to terminal width.
Plain/piped input retains the numbered fallback; `/resume <id>` still works directly.

`/new` saves the current cursor, creates a fresh log with the selected model and a new
snapshot of project instructions, and enters it in place. The old nonempty log remains
available through `/resume`; an empty abandoned log is removed. No workspace files are
changed. The new session starts at root with no nodes, grafts, name, usage or cost. The
terminal clears its display/scrollback, queued inputs, attachment draft and activity state;
Canvas clears its transcript, draft and view. Like other terminal commands, `/new` entered
during a turn waits for that turn to finish before switching, then discards remaining
queued inputs. Canvas rejects it while a turn is running.

### 7.1 Configuration is a conversation, not an export

Two things have to be true before a turn can run: a provider with a key, and an active
model. `/provider` asks for the first (vendor, endpoint - editable, since several vendors
run a separate Chinese endpoint with separate keys - key, and the model ids that vendor
offers you); `/model` picks the second, from every model listed across every configured
provider, as one list. A first run walks both in order.
The provider selector starts with an empty number field: users may type a number or
use Up/Down to highlight a row and fill its number, then Enter confirms. Valid typed
numbers also highlight their row; invalid/empty input stays in the selector. Existing
providers open an Up/Down/Enter action menu for refreshing, editing or clearing their
saved configuration. Clearing removes that provider's saved URL, key and model list,
and its active selection if applicable, but preserves other providers, history, prices
and environment variables. Esc at either menu exits without changes.
In a terminal `/model` uses Up/Down and Enter, initially focused on the active model;
plain/piped input retains the numbered fallback. Both configuration commands advertise
Esc cancellation, which exits only the command without saving partial input. Ctrl-C
inside these menus also cancels the command, rather than exiting the session.
The model-id prompt defaults to every model listed
for that provider in the pricing table: Enter accepts all, while a comma-separated
input selects a subset or supplies custom ids. The pricing table takes precedence
over a saved subset; if it has no models for the provider, the saved list is used.

Provider setup obtains default model ids from its configured pricing catalog. A preset
may explicitly share another preset's catalog (for example `deepseek-anthropic` uses
`deepseek`), without changing the selected protocol, endpoint or credentials. Qualified
price keys are stripped to model ids in setup; the model selector resolves prices through
the same `Pricing.find(provider/model)` lookup as requests and the ledger. Unrelated
regions and subscription plans keep distinct catalogs. DeepSeek's packaged rates are
fixed peak USD rates; time-of-day discounts are not modelled.

Selecting an already configured provider offers model/price refresh by default, editing
its endpoint, key and model list, or clearing its configuration. Refresh reads only model rows from kaipi's
maintained online `master/kaipi/pricing.toml`, without sending API credentials. This is
not a live vendor inventory: new releases become available when that catalog is updated.
The preview is saved only on Enter; cancellation, invalid data and network failure leave
configuration unchanged. Refresh preserves existing/custom model ids and the active model,
adds catalog ids to `/model`, and stores prices separately in `~/.config/kaipi/catalog.json`.
Downloaded rows supersede packaged prices, while explicit project/user pricing overrides
remain authoritative. Remote endpoint declarations are never imported. The current CLI
session reloads pricing and provider instances after configuration, without restarting.

Credentials go to `~/.config/kaipi/auth.toml`, mode 600, never into a project: a key in a
repository is a key in someone's clone. The environment still wins over the file, so a
one-off export overrides a saved key without unsaving it. A missing key is refused with a
sentence naming the command that fixes it, rather than by letting the vendor answer 401.
Anthropic clients receive only the selected `api_key` SDK credential. Compatible
gateways also receive a Bearer header through `default_headers`; it is not a second
SDK auth mode. Unrelated Anthropic token/profile environment settings do not replace
the explicitly selected provider key.

The agent's bash is the one way a key could leave that file, so it is closed from both
ends. Commands run without the variables a key can arrive in (every preset's
`api_key_env`, those of the providers `pricing.toml` declares, and `ANTHROPIC_AUTH_TOKEN`),
and a command's output has every known key - saved, exported, or handed to a provider -
replaced by `[kaipi: API key redacted]` before it is truncated, logged
or sent. There is no sandbox, so a command can still read `auth.toml`; what it cannot do is
hand the key back into a session log or a request. The file itself is created mode 600
before the key is written into it.

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
