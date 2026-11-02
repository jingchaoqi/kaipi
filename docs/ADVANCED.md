# Advanced notes

Everything here used to live in the README. It is the detail you need when you are
running kaipi against a real endpoint, or working on kaipi itself.

## Local HTTP API

The canvas is served by the same process that runs the session, on `127.0.0.1` with a
random port (`KAIPI_PORT` overrides it). There is no separate server to start and nothing
leaves the machine.

The API can run bash, so it is guarded accordingly: a loopback `Host` with a matching
port, a matching `Origin`, a per-run token that only the URL kaipi opened carries, and a
JSON content type. A page you already had open, a page that guessed the port, and another
process on the machine are all refused.

Endpoints: `GET /api/state`, `GET /api/node/<id>`, `GET /api/preview?src=&tools=`,
`GET /api/rewind?id=`, `GET /api/events` (SSE), `POST /api/turn {text, explore}`,
`/api/command {line}`, `/api/go`, `/api/archive`, `/api/restore`, `/api/pin`,
`/api/rewind {id, mode}`, `/api/pending {edges}`, `/api/reset`, `/api/handoff`,
`/api/stop`.

The canvas folds a command's output to its first four lines and expands it on a click; the
terminal prints the same four and says how many it kept back, since scrollback cannot be
folded open again once printed. Neither truncates what is stored: the whole output is in
the node payload, and `GET /api/node/<id>` returns it.

`/api/stop` is the one call accepted while a turn is running - everything else is refused
with 409 until it ends. It sets the turn's stop flag; the turn ends at the next step
boundary, the running command is killed with its process group, and the node is recorded
as `aborted`: billed, kept for display, never assembled into another request.

## Surfaces and the lock

A session is open on exactly one surface at a time. The terminal prompt closes while the
canvas is open and reopens when the canvas sends `/cli`; a second `kaipi` in the same
directory is refused while either surface holds the session (`.kaipi/lock.json`).

## The exploration guard

Explorations are read-only by convention, not by sandbox. kaipi snapshots the working
tree (a git tree object plus HEAD) before the turn, re-checks after every command, warns
in the node if anything changed, and offers a one-shot revert.

## Rewind internals

Every turn records the git tree of the working tree when it ended and the list of files it
changed. Snapshots go through kaipi's private index under `.kaipi/`, pinned at
`refs/kaipi/<session>/<node>`; your index, your stash and your branches are untouched.

`kaipi rewind <id>` offers the same three choices as Claude Code's `/rewind`: conversation
only (equivalent to `go`), code only, or both. A code rewind restores **only the files
touched by turns after that node**; files the agent never changed, including your own
manual edits elsewhere, are left alone. The pre-rewind tree is pinned at `refs/kaipi/undo`.
A non-git directory gets no snapshots and no code rewind.

## Cache breakpoints

Anthropic `cache_control` breakpoints sit at four fixed places: end of the system prompt,
the fork point (the nearest ancestor with more than one live child), the end of the
lineage, and the end of the request. Sibling explorations therefore share the cached
prefix up to their fork point, and explorations never modify the system prompt, so they
never leave the trunk's cache namespace.

## Build

```sh
uv build      # dist/kaipi-0.1.0-py3-none-any.whl and dist/kaipi-0.1.0.tar.gz
```

Pure Python: nothing is compiled. The wheel must contain `kaipi/canvas.html` and
`kaipi/pricing.toml` — an installed kaipi has no repo root to find them in, and CI installs
the built wheel into a clean environment and imports both, because a missing package file
crashed kaipi on its first run once already.

## Checks

```sh
uv run ruff check . && uv run mypy --strict kaipi tests && uv run pytest -q
```

Non-goals: MCP, sub-agents, permission prompts, plan mode, plugins, RAG, memory,
multi-agent, semantic merge, agent frameworks.

## What has actually been verified against a live endpoint

Being honest about this matters more than the number of green checks, because the whole
project is a bet on prompt caching and every test below the API is a fake talking to a
fake.

| | Verified live | How |
|---|---|---|
| requests are accepted, tool loop, frozen payload replay | yes | Moonshot `kimi-k2.7-code`, 2026-09-05 |
| the ledger's four token counts equal the API's own usage | yes | same run, to the token |
| Anthropic cache breakpoints, `cache_creation` vs `cache_read` | **no** | needs an Anthropic key |
| sibling explorations sharing the fork-point prefix | **no** | same |
| preserved thinking surviving a tool loop and a replay | **no** | first-party Anthropic only |
| request shapes for all four wire protocols | mock only | `scripts/mockapi.py`, strictly validated |
| a gateway accepting the custom bash tool and calling it | yes | Moonshot `/anthropic`, 2026-09-08: six commands in one turn |

A mock run proves kaipi's requests are well formed and its prefixes are stable and shared.
It cannot tell you what a real cache does. Anyone with an Anthropic key can close the
remaining rows in one command; until someone does, they are open.

## Real-API smoke test

The unit tests use a fake provider, so they cannot tell you whether prompt caching, the
cache breakpoints or reasoning replay actually work against a live endpoint. That is what
`scripts/smoke.py` is for. It spends real money: it builds a throwaway git repo, runs the
scenario from the design (two explorations from one fork, archive one, graft the other
onto the trunk), and then checks the things only a real API can answer — the ledger's four
token counts against the API's own usage, that a second trunk turn reads the first back
out of the cache, that sibling explorations share the fork-point prefix, that reasoning
state survives a tool loop and a replay, and that a code rewind restores the right files.

The smoke script reads the key from the environment, not from `/provider`'s file, so that
a test run can never be steered by whatever the developer happens to have configured:

```sh
export ANTHROPIC_API_KEY=...
uv run python scripts/smoke.py                      # default model from pricing.toml
uv run python scripts/smoke.py --model gpt-5.6-terra --keep
```

Exit code 0 means every check passed; checks that cannot apply to the chosen provider are
reported SKIP. Run it for each provider you intend to support.

## Running the smoke test without a key

`--mock` runs the same scenario against `scripts/mockapi.py`: the real provider code over
real HTTP to a local endpoint that validates request shapes strictly (a malformed tool
schema, an unpaired `tool_result`, a fifth cache breakpoint or a system prompt that changed
mid-conversation are all 400s), implements a genuine byte-keyed prefix cache with the
20-position lookback, and binds reasoning signatures to the conversation prefix so an
accidental history edit is rejected the way preserved thinking rejects it.

```sh
uv run python scripts/smoke.py --mock                           # Anthropic Messages
uv run python scripts/smoke.py --mock --model gpt-5.6-terra     # OpenAI Responses
uv run python scripts/smoke.py --mock --model gemini-3.7-flash  # Gemini
uv run python scripts/smoke.py --mock --model deepseek-v4-flash # OpenAI Chat
```

All four are in the test suite. A mock run proves kaipi's requests are well formed and its
prefixes are stable and shared; it cannot tell you whether the live API accepts them or
what its cache really does. Only a key answers that.

`scripts/canvas_e2e.py` drives the canvas in a real Chromium against the same mock.

## Attachments

Terminal attachment gestures are explicit: paste an entire file selection (paths may be
quoted, shell-escaped, or file:// URLs) or press Ctrl-V to read the clipboard. Terminal
Cmd-V / Ctrl-Shift-V is handled by the terminal emulator and may transmit text only;
use Ctrl-V for raw clipboard images. A remote SSH process reads its own machine's
clipboard, not the local desktop. macOS uses its native pasteboard; Linux uses
`wl-paste` (wl-clipboard) or `xclip`. If neither is available, paste file paths or use
Canvas. Attachments appear as editable markers; Backspace at the end removes a marker.
Deleting its marker excludes the attachment from submission. Only a whole pasted file
selection is recognized: mentioning a path within normal prose does not read that file.

Canvas supports ordinary clipboard image/file paste, a file chooser and external file
drop. These files share the question's next node, and attach separately from node graft
chips. Expanded records show images and document/text attachment entries.

The same backend detects file contents for both surfaces. Pillow and pillow-heif decode
raster images including HEIC/HEIF, AVIF, TIFF and BMP, even with no extension or an
incorrect filename. Valid, already-compatible PNG/JPEG/WebP are preserved when they
need no transformations. Other images are normalized to PNG, with JPEG fallback for
large opaque images. EXIF orientation is corrected; supported ICC profiles become sRGB;
alpha is retained. Images may be downscaled to fit pixel and encoded-payload limits.
Conversions are described in the draft and frozen into the message, with the original
filename. Multi-image files are expanded in order, preserving all frames and available
frame durations; they are rejected explicitly when limits would require dropping frames.
Drafts show compact `[image1]` / `[file1]` markers; expanded history shows images.

PDFs remain native documents so layout and diagrams are available to a capable model.
UTF-8 code/text stays verbatim. Other local documents use MarkItDown converters (Word,
legacy Excel, PowerPoint, EPUB and related text formats); OOXML workbooks use a dedicated
reader to retain formula source as well as sheet names, values and dates. Container
contents identify OOXML files regardless of extension. Raw ZIP recursion, cloud clients,
URL converters and third-party converter discovery are disabled. No macros, formulas or
external document links are executed. Unsupported/corrupt/encrypted content reports a
conversion failure and keeps the draft. Document extraction does not promise original
page layout or embedded charts/images; export to PDF when those are important.

Limits: eight original files per message, 20 MiB per original and 40 MiB original total.
Normalized image/PDF payloads allow 5 MiB per block and 10 MiB total; PDFs above 5 MiB
must be reduced before attaching. A file can contain at most eight image frames and a
message at most sixteen images. Decoding permits 64 million pixels per frame and a
4096-pixel long edge in normalized output. Extracted text permits 128 KiB per file;
document archives permit 20 MiB uncompressed content. OOXML Excel is limited to 50,000
cells overall and 10,000 rows per sheet. The local JSON request cap is 56 MiB to allow
base64 overhead. Conversion is local and runs off the input loop; only the normalized
snapshot is sent to the selected model and stored in the session log. History never
re-reads original files or repeats conversion after an upgrade.

The implementation is isolated in `kaipi/attachments/`. Its only parsing boundary is
`parse(name, raw_bytes) -> (content_blocks, summary)`; a host can replace
`kaipi.attachments.parse` at startup with a function of the same signature, raising
`ValueError` for unsupported input. The default implementation loads image/document
libraries only when needed. Both UIs prepare snapshots before the agent receives them;
changing the parser never changes existing drafts or logged payloads.
