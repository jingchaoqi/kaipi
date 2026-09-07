# Providers

kaipi separates the **wire protocol** (how a request is shaped) from the **vendor** (which
endpoint and which key). This is the split that pi's `pi-ai` (`api` field per model), Codex
(`wire_api = "chat" | "responses"` per provider) and opencode's models.dev registry all
arrived at; kaipi keeps it to a single TOML table and one factory function.

## Wire protocols (`api`)

| `api` | Implementation | Notes |
|---|---|---|
| `anthropic` | `providers/anthropic.py` | Messages API via the official SDK. Explicit `cache_control` breakpoints (system end, fork point, lineage end, request end), `cache_creation_input_tokens` / `cache_read_input_tokens` mapped to the ledger, preserved-thinking `drop_block` guard. |
| `openai-responses` | `providers/openai_responses.py` | Responses API, stateless (`store: false`, `include: ["reasoning.encrypted_content"]`). Reasoning items are stored in the node payload and replayed verbatim so chains of tool calls keep their reasoning. `input_tokens_details.cached_tokens` → cache read; there is no write premium. |
| `openai-chat` | `providers/openai_compat.py` | Chat Completions. Used by every OpenAI-compatible vendor. `prompt_tokens_details.cached_tokens` or DeepSeek's `prompt_cache_hit_tokens` → cache read. |
| `gemini` | `providers/gemini.py` | `generateContent` over REST with an API key. Thought signatures on function-call parts are stored on the block and sent back verbatim; `cachedContentTokenCount` → cache read; `thoughtsTokenCount` is billed as output. `countTokens` backs graft previews. |

All four expose the same two calls, `complete()` and `count_tokens()`, and return the
same `Reply` (raw assistant blocks, stop reason, `Usage`). Node payloads are stored in
the Anthropic block shape; each protocol converts at its boundary.

## Vendors (`provider`)

Built in, no configuration needed beyond the key. Vendors that speak two protocols get
two presets; the `-anthropic` one is the endpoint each vendor documents for Claude Code.

| provider | api | base_url | key |
|---|---|---|---|
| `anthropic` | anthropic | SDK default | `ANTHROPIC_API_KEY` |
| `openai` | openai-responses | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `gemini` | gemini | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` |
| `deepseek` | openai-chat | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| `deepseek-anthropic` | anthropic | `https://api.deepseek.com/anthropic` | `DEEPSEEK_API_KEY` |
| `kimi` | openai-chat | `https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` |
| `kimi-anthropic` | anthropic | `https://api.moonshot.ai/anthropic` | `MOONSHOT_API_KEY` |
| `glm` | openai-chat | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY` |
| `glm-anthropic` | anthropic | `https://api.z.ai/api/anthropic` | `ZAI_API_KEY` |
| `opencode` (Zen) | openai-chat | `https://opencode.ai/zen/v1` | `OPENCODE_API_KEY` |
| `opencode-anthropic` | anthropic | `https://opencode.ai/zen` | `OPENCODE_API_KEY` |
| `opencode-go` | openai-chat | `https://opencode.ai/zen/go/v1` | `OPENCODE_API_KEY` |
| `opencode-go-anthropic` | anthropic | `https://opencode.ai/zen/go` | `OPENCODE_API_KEY` |
| `qwen` | openai-chat | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| `openrouter` | openai-chat | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `groq` | openai-chat | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `ollama` | openai-chat | `http://localhost:11434/v1` | `OLLAMA_API_KEY` (unused) |

## Configuring one without exporting anything

`kaipi provider` (or `/provider` in a session) asks for the vendor, its endpoint - editable,
because several vendors run a separate Chinese endpoint with separate keys - the key, and
the model ids you intend to use from it. `kaipi model` then switches between every model
you listed, across all configured vendors, and activates one.

That is stored in `~/.config/kaipi/auth.toml`, mode 600, never in the project. The
environment still wins over it, so a one-off `MOONSHOT_API_KEY=... kaipi` overrides a saved
key without unsaving it.

`<PROVIDER>_BASE_URL` (dashes as underscores) overrides any base URL:
`KIMI_BASE_URL=https://api.moonshot.cn/v1` and `GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4`
switch to the China endpoints; `ANTHROPIC_BASE_URL` puts a gateway in front of Anthropic.

### Vendor notes

- **DeepSeek**: `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp` (the
  old `deepseek-chat` / `deepseek-reasoner` aliases were retired 2026-07). Cache hits are
  reported as `prompt_cache_hit_tokens` on the chat endpoint and as Anthropic-shaped usage
  on `/anthropic`; both feed the ledger. Off-peak pricing is not modelled.
- **Kimi**: `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `kimi-k2.5` on the OpenAI-style
  endpoint. On the Anthropic-style endpoint the 1M-context id is `kimi-k3[1m]`:
  `KAIPI_MODEL=kimi-anthropic/kimi-k3[1m]`. The China platform is a separate endpoint with
  separate keys and its own model list - a key from one does not work on the other - so
  point kaipi at it explicitly:
  `KIMI_BASE_URL=https://api.moonshot.cn/v1 MOONSHOT_API_KEY=... KAIPI_MODEL=kimi-k2.7-code`.
  Verified live on 2026-09-05 against `api.moonshot.cn` with `kimi-k2.7-code`: requests,
  the bash tool round-trip and the four usage counts all check out, and the endpoint returns
  no cache fields at all on a short prompt, so the ledger reads 0 cache tokens rather than
  guessing. Reasoning tokens arrive inside `completion_tokens`, so output is not
  under-reported.
- **GLM**: `glm-5.2`, `glm-5` pay-per-token; the Coding Plan is a flat subscription on the
  Anthropic-style endpoint with ids like `glm-5.2[1m]`: `KAIPI_MODEL=glm-anthropic/glm-5.2[1m]`.
- **OpenCode Go / Zen**: one key for many vendors. Go is a flat $10/month, so its models
  are priced 0 in `pricing.toml` and the ledger reports tokens only. Model ids come from
  `https://opencode.ai/zen/go/v1/models` (kimi-k3, deepseek-v4-pro, glm-5.3, gpt-5.6-luna,
  minimax-m3, qwen3.8-max, …). Claude models on Zen go through `opencode-anthropic`.
- **Anthropic-compatible gateways** (`*-anthropic`): kaipi still sends its four
  `cache_control` breakpoints and reads Anthropic-shaped usage, but omits the
  preserved-thinking beta header and `thinking.block_binding`, which only Anthropic
  understands; the gateway's own thinking default applies.

## Where pricing.toml comes from

The first of `.kaipi/pricing.toml` in the project, `~/.config/kaipi/pricing.toml`, then the
copy shipped inside the package. Copy the packaged one to either location to correct a price
or add a vendor without editing site-packages.

**A project file's `[providers]` table is ignored**, with a warning. That table names an
endpoint URL and an environment variable to send there as a credential; a repository you
cloned must not get to choose either, or it could collect your API key and then answer as
the model - whose tool calls kaipi runs. Prices and the model choice from a project file are
honoured; endpoints come only from your user config or the packaged defaults.

## Choosing a model

- A model listed in `pricing.toml` names its provider and its four prices:
  `KAIPI_MODEL=deepseek-v4-flash`, `KAIPI_MODEL=kimi-k3`, `KAIPI_MODEL=glm-5.2`.
- Anything else works as `<provider>/<model id>` and is priced at 0 (the CLI warns):
  `KAIPI_MODEL=ollama/qwen3:32b`, `KAIPI_MODEL=openrouter/anthropic/claude-sonnet-5`.
- One session, one model: caches are model-scoped and thinking blocks are model-bound,
  so the model is fixed at `session_started`.

## What differs per protocol, and what the ledger does about it

| | anthropic | openai-responses | openai-chat | gemini |
|---|---|---|---|---|
| Cache control | explicit breakpoints, 5-min TTL | automatic prefix | automatic prefix (vendor-dependent) | implicit (2.5+) |
| Cache write cost | 1.25× | none | none | none |
| Reasoning carried across tool calls | thinking blocks (signed) | reasoning items (encrypted) | none (vendor-specific) | thought signatures |
| Refusal signalled by | `stop_reason: refusal` | `refusal` content part | `finish_reason: content_filter` | `finishReason: SAFETY` etc. |
| Token counting for previews | `count_tokens` | estimate | estimate | `countTokens` |

Bedrock and Vertex for Claude are not wired: the Anthropic SDK offers dedicated clients
for them but they pull in extra dependencies. `ANTHROPIC_BASE_URL` covers gateways.

## Verifying a protocol without a key

`scripts/mockapi.py` implements all four protocols strictly enough to be worth testing
against: it rejects anything that deviates from the documented request shape, and its
prefix cache is keyed on the actual bytes, with the same 20-position lookback the real one
uses. `uv run python scripts/smoke.py --mock --model <id>` therefore answers "are kaipi's
requests well formed, and are its prefixes stable and shared" for any protocol. It cannot
answer "does the live endpoint accept this".

What that run showed for each protocol, on the same scenario:

| protocol | siblings share the fork prefix | reasoning state replayed |
|---|---|---|
| anthropic | yes, byte-identical reads (breakpoints land on the marked fork) | thinking blocks, signature verified on every replay |
| openai-responses | yes, and the second reads further - the first warmed the shared read-only guard block | reasoning items with encrypted_content |
| gemini | yes, byte-identical reads | thoughtSignature on the function-call part |
| openai-chat | yes, byte-identical reads | none: the protocol carries no reasoning state |

## Verifying a new vendor

1. `kaipi provider` for the vendor, or `KAIPI_MODEL=<provider>/<model> uv run kaipi` to
   bypass the config file entirely; then ask for a two-command task.
2. Check the ledger line after the turn: uncached / cache read / output should match the
   vendor's dashboard for that request.
3. Check that the second turn shows cache reads at all; if not, the vendor does not
   report caching through the fields kaipi reads, and the ledger will under-report.

Prices in `pricing.toml` for non-Anthropic vendors come from public price pages and
carry the date they were taken; verify before relying on the totals.
