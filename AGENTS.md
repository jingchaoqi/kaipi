# kaipi — project instructions for agents working in this repo

- Python 3.11+, managed with `uv`. Run checks with:
  `uv run ruff check . && uv run mypy --strict kaipi tests && uv run pytest -q`
- There is no hard line limit any more, but small is still the point: prefer deleting a
  special case to adding one, and do not add an abstraction for generality alone. The core
  library is around 3.7k lines (`wc -l kaipi/*.py kaipi/providers/*.py`); if a change makes
  it much larger, that is worth a second look rather than a rule violation.
- `docs/CONCEPTS.md` is the constitution. Change it in the same commit as any semantic change.
- No agent frameworks, no MCP, no sub-agents, no plugin system. One tool (`bash`), one
  provider interface (four wire protocols behind it, see `docs/PROVIDERS.md`), one storage
  format (JSONL event log).
- Session state is a pure fold over the event log; never add hidden state outside it
  (the CLI cursor in `.kaipi/cursor.json` is UI state, not session state).
- Changing anything about context assembly, cache breakpoints, usage extraction or the
  ledger means re-running `scripts/smoke.py` against a real endpoint; the fake-provider
  tests cannot catch a regression there.
