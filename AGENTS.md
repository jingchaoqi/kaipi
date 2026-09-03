# kaipi — project instructions for agents working in this repo

- Python 3.11+, managed with `uv`. Run checks with:
  `uv run ruff check . && uv run mypy --strict kaipi tests && uv run pytest -q`
- The core library (`kaipi/`, excluding tests and `canvas.html`) must stay under 3000
  lines. Check with `wc -l kaipi/*.py kaipi/providers/*.py`.
- `docs/CONCEPTS.md` is the constitution. Change it in the same commit as any semantic change.
- No agent frameworks, no MCP, no sub-agents, no plugin system. One tool (`bash`), one
  provider interface (four wire protocols behind it, see `docs/PROVIDERS.md`), one storage
  format (JSONL event log).
- Session state is a pure fold over the event log; never add hidden state outside it
  (the CLI cursor in `.kaipi/cursor.json` is UI state, not session state).
- Changing anything about context assembly, cache breakpoints, usage extraction or the
  ledger means re-running `scripts/smoke.py` against a real endpoint; the fake-provider
  tests cannot catch a regression there.
