# kaipi (开辟)

A tiny, open-source coding agent that is extremely frugal with tokens. Not a smarter
agent loop — a **DAG-shaped session** plus a **token ledger as a first-class citizen**.

Pi-style minimal kernel · DAG-native sessions · the ledger is the product.

## Why a DAG

A linear chat has one context, and every turn re-pays all of it. When you explore a
dead end, that dead end stays in the context forever (or you compact and lose detail).

kaipi makes the exploration structure explicit:

- Every turn is a **node** (user input + all tool calls + final answer, frozen).
- You can open several **explorations** from the same node, abandon the ones that go
  wrong (`archive`, reversible), and **graft** the conclusion of a good one back onto
  the **trunk** — a snapshot of its result, not its transcript.
- The trunk only ever receives what you explicitly drag in.

Details in [`docs/CONCEPTS.md`](docs/CONCEPTS.md).

## What "trunk burden" is

The trunk is the path you are actually building on. Its **burden** is the number of
tokens the model re-reads on every trunk turn. kaipi prints it after each turn and
previews how each operation changes it (`trunk burden: 31.2k -> 32.0k (+0.8k)`).

## Explorations cost simple interest; the trunk costs compound interest

Work done on an exploration is paid once. The same work done on the trunk is paid on
every subsequent turn. `kaipi ledger` shows, per exploration, what it cost and how many
tokens it kept out of the trunk. See [`docs/LEDGER.md`](docs/LEDGER.md).

Cache breakpoints are placed so sibling explorations share the cached prefix up to
their fork point; explorations never modify the system prompt, so they never leave the
trunk's cache namespace.

## Install & run

```sh
uv sync
export ANTHROPIC_API_KEY=...          # or OPENAI_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY ...
export KAIPI_MODEL=claude-opus-5      # default from pricing.toml; or <provider>/<model>
uv run kaipi                          # interactive session in the current directory
```

Four wire protocols are supported natively (Anthropic Messages, OpenAI Responses, OpenAI
Chat Completions, Gemini generateContent). Preconfigured vendors: anthropic, openai,
gemini, deepseek, kimi, glm, opencode / opencode-go (each of the last four in both an
OpenAI-style and an Anthropic-style form), qwen, openrouter, groq, ollama; any other
OpenAI-compatible endpoint is one TOML block away. See [`docs/PROVIDERS.md`](docs/PROVIDERS.md).

Commands (also available as `/tree`, `/go`, … inside the session):

```
kaipi                       interactive session (resumes the latest); --new starts fresh
kaipi tree                  session tree: trunk, current node, live/archived, tokens per node
kaipi go <id>               continue from a node; the next input opens a branch there
kaipi graft <id> [--depth leaf|leaf+summary|branch] [--with-tool <tool_use id>...]
kaipi archive <id>          archive a subtree (prints the trunk-burden change)
kaipi restore <id>
kaipi rewind <id> [--mode both|code|conversation]
                            rewind like Claude Code: only agent-touched files are restored
kaipi trunk [pin <id>]
kaipi ledger
kaipi sessions
kaipi canvas                the same session on a local web canvas (drag = graft)
```

Node ids can be abbreviated to any unique prefix or suffix (the tree shows the last 6).

`/explore <text>` opens an exploration from the trunk leaf without extending the trunk
(the trunk is pinned in place).

## The canvas

```sh
uv run kaipi canvas        # open the session in the browser instead of the terminal
```

Or type `/canvas` inside a terminal session. The same process serves a local page
(`127.0.0.1`, random port unless `KAIPI_PORT` is set) where the session is a diagram:

- click a node to continue from it; the transcript pane shows that lineage;
- **drag a node onto the input box to graft it** — a chip appears with the depth
  selector, the tool-output picker and the token preview for all three depths;
- drag a node onto the tray to archive its subtree; right-click for restore / pin;
- right-click → 回退到这里… shows the files a code rewind would restore, then lets you
  rewind conversation, code, or both;
- slash commands work in the input box too; `/cli` hands the session back to the
  terminal, which is waiting in the same process.

A session is open on exactly one surface at a time. The terminal prompt closes while
the canvas is open and reopens when the canvas sends `/cli`; a second `kaipi` in the
same directory is refused while either surface holds the session (`.kaipi/lock.json`).
There is no separate server to run and nothing leaves the machine. The API runs bash, so it
is guarded accordingly: loopback Host and port, a matching Origin, a per-run token that only
the URL kaipi opened carries, and a JSON content type. A page you have open, a page that
guessed the port, and another process on the machine are all refused.

Endpoints, all under `127.0.0.1`: `GET /api/state`, `GET /api/node/<id>`,
`GET /api/preview?src=&tools=`, `GET /api/rewind?id=`, `GET /api/events` (SSE),
`POST /api/turn {text, explore}`, `/api/command {line}`, `/api/go`, `/api/archive`,
`/api/restore`, `/api/pin`, `/api/rewind {id, mode}`, `/api/pending {edges}`, `/api/reset`,
`/api/handoff`.

Explorations are read-only by convention: kaipi snapshots the working tree (a git tree
object plus HEAD) before the turn, re-checks after every command, warns if anything
changed, and offers a one-shot revert.

## Rewind

Every turn records the git tree of the working tree when it ended and the files it
changed (snapshots go through kaipi's private index under `.kaipi/`, pinned at
`refs/kaipi/<session>/<node>`; your index, stash and branches are untouched). `kaipi rewind
<id>` then offers the same three choices as Claude Code's `/rewind`: conversation only
(`go`), code only, or both. A code rewind restores **only the files touched by turns after
that node**; files the agent never changed, including your manual edits elsewhere, are left
alone. The pre-rewind tree is pinned at `refs/kaipi/undo`. Non-git directories get no
snapshots and no code rewind.

## Development

```sh
uv run ruff check . && uv run mypy --strict kaipi tests && uv run pytest -q
```

### Real-API smoke test

The unit tests use a fake provider, so they cannot tell you whether prompt caching, the
cache breakpoints or reasoning replay actually work against a live endpoint. That is what
`scripts/smoke.py` is for. It spends real money: it builds a throwaway git repo, runs the
scenario from the design (two explorations from one fork, archive one, graft the other onto
the trunk), and then checks the things only a real API can answer — the ledger's four token
counts against the API's own usage, that a second trunk turn reads the first back out of
the cache, that sibling explorations share the fork-point prefix, that reasoning state
survives a tool loop and a replay, and that a code rewind restores the right files.

```sh
export ANTHROPIC_API_KEY=...
uv run python scripts/smoke.py                      # default model from pricing.toml
uv run python scripts/smoke.py --model gpt-5.6-terra --keep
```

Exit code 0 means every check passed; checks that cannot apply to the chosen provider are
reported SKIP. Run it for each provider you intend to support.

Without a key, `--mock` runs the same scenario against `scripts/mockapi.py`: the real
provider code over real HTTP to a local endpoint that validates request shapes strictly
(a malformed tool schema, an unpaired `tool_result`, a fifth cache breakpoint or a system
prompt that changed mid-conversation are all 400s), implements a genuine byte-keyed prefix
cache with the 20-position lookback, and binds reasoning signatures to the conversation
prefix so an accidental history edit is rejected the way preserved thinking rejects it.

```sh
uv run python scripts/smoke.py --mock                      # Anthropic Messages
uv run python scripts/smoke.py --mock --model gpt-5.6-terra    # OpenAI Responses
uv run python scripts/smoke.py --mock --model gemini-3.7-flash # Gemini
uv run python scripts/smoke.py --mock --model deepseek-v4-flash # OpenAI Chat
```

All four are in the test suite. A mock run proves kaipi's requests are well formed and its
prefixes are stable and shared; it cannot tell you whether the live API accepts them or what
its cache really does. Only a key answers that.

The core library must stay under 3000 lines (`wc -l kaipi/*.py kaipi/providers/*.py`).
Non-goals: MCP, sub-agents, permission prompts, plan mode, plugins, RAG, memory,
multi-agent, semantic merge, web UI, agent frameworks.
