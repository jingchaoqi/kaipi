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
export ANTHROPIC_API_KEY=...          # or OPENAI_API_KEY + OPENAI_BASE_URL for OpenAI-compatible
export KAIPI_MODEL=claude-opus-5      # default from pricing.toml
uv run kaipi                          # interactive session in the current directory
```

Commands (also available as `/tree`, `/go`, … inside the session):

```
kaipi                       interactive session (resumes the latest); --new starts fresh
kaipi tree                  session tree: trunk, current node, live/archived, tokens per node
kaipi go <id>               continue from a node; the next input opens a branch there
kaipi graft <id> [--depth leaf|leaf+summary|branch] [--with-tool <tool_use id>...]
kaipi archive <id>          archive a subtree (prints the trunk-burden change)
kaipi restore <id>
kaipi trunk [pin <id>]
kaipi ledger
kaipi sessions
```

Node ids can be abbreviated to any unique prefix or suffix (the tree shows the last 6).

Explorations are read-only by convention: kaipi snapshots `git status`/`git diff`
before the turn, warns if the tree changed, and offers a one-shot revert.

## Development

```sh
uv run ruff check . && uv run mypy --strict kaipi tests && uv run pytest -q
```

The core library must stay under 2000 lines (`wc -l kaipi/*.py kaipi/providers/*.py`).
Non-goals: MCP, sub-agents, permission prompts, plan mode, plugins, RAG, memory,
multi-agent, semantic merge, web UI, agent frameworks.
