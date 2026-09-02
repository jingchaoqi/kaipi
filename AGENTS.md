# kaipi — project instructions for agents working in this repo

- Python 3.11+, managed with `uv`. Run checks with:
  `uv run ruff check . && uv run mypy --strict kaipi tests && uv run pytest -q`
- The core library (`kaipi/`, excluding tests) must stay under 2000 lines. Check with
  `wc -l kaipi/*.py kaipi/providers/*.py`.
- `docs/CONCEPTS.md` is the constitution. Change it in the same commit as any semantic change.
- No agent frameworks, no MCP, no sub-agents, no plugin system. One tool (`bash`), one
  provider interface, one storage format (JSONL event log).
- Session state is a pure fold over the event log; never add hidden state outside it
  (the CLI cursor in `.kaipi/cursor.json` is UI state, not session state).
