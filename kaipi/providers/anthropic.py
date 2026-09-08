"""Anthropic Messages API: fixed cache breakpoints, usage extraction, preserved-thinking guard."""

from __future__ import annotations

from typing import Any

import anthropic

from kaipi.ledger import estimate_tokens
from kaipi.model import Message, Usage
from kaipi.providers import BASH_PARAMETERS, BASH_TOOL_DESCRIPTION, Reply

BINDING_BETA = "thinking-binding-controls-2026-08-01"
MAX_TOKENS = 32_000
# kaipi never edits history; if it ever did, the API drops the affected thinking blocks
# instead of failing the request, and the ledger counts them.
PREFIX_MISMATCH = "drop_block"
# First-party Anthropic implements bash as a server-side tool, declared by type and
# schema-less. A compatible gateway does not: it sees a tool type it has never heard of,
# hands the model nothing, and the model answers "I will look into it" and stops. So a
# gateway gets the ordinary custom tool that every Messages implementation understands.
BASH_TOOL: dict[str, Any] = {"type": "bash_20250124", "name": "bash"}
BASH_TOOL_COMPAT: dict[str, Any] = {
    "name": "bash",
    "description": BASH_TOOL_DESCRIPTION,
    "input_schema": BASH_PARAMETERS,
}


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        *,
        adaptive_thinking: bool = True,
        base_url: str | None = None,
        api_key: str | None = None,
        compat: bool = False,
    ) -> None:
        """`compat`: a third-party Anthropic-compatible gateway (Kimi, GLM, DeepSeek, OpenCode
        Zen). They accept cache_control and report Anthropic-shaped usage, but not the
        preserved-thinking beta or `thinking.block_binding`, so those are left out and the
        gateway's own thinking default applies."""
        self.model = model
        # Gateways document either header; the SDK sends x-api-key for api_key and
        # Authorization: Bearer for auth_token, so give them both when a key is supplied.
        self.client = anthropic.Anthropic(
            base_url=base_url, api_key=api_key or None, auth_token=api_key or None
        )
        self.compat = compat
        # Preserved thinking is first-party Anthropic only, and per-model (pricing.toml).
        self.thinking = adaptive_thinking and not compat

    def _request(self, system: str, messages: list[Message], cache_points: list[int]) -> Any:
        # Copy-on-write: only the <= 4 marked messages are rebuilt, the rest are shared.
        msgs = list(messages)
        for i in sorted({*cache_points, len(msgs) - 1}):  # <= 3 fixed + the moving end = 4 max
            blocks = msgs[i]["content"]
            if isinstance(blocks, list) and blocks:
                marked = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
                msgs[i] = {**msgs[i], "content": [*blocks[:-1], marked]}
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": [BASH_TOOL_COMPAT if self.compat else BASH_TOOL],
            "messages": msgs,
        }
        if self.thinking:
            body["thinking"] = {
                "type": "adaptive",
                "block_binding": {"prefix_mismatch_behavior": PREFIX_MISMATCH},
            }
        return body

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        body = self._request(system, messages, cache_points)
        # The SDK takes these three as arguments; everything else rides along as extra_body.
        sdk = {k: body.pop(k) for k in ("model", "max_tokens", "messages")}
        with self.client.messages.stream(
            **sdk,
            extra_body=body,
            extra_headers={"anthropic-beta": BINDING_BETA} if self.thinking else {},
        ) as stream:
            msg = stream.get_final_message()
        content = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
        u = msg.usage
        dropped = len(getattr(msg, "input_transformations", None) or [])
        return Reply(
            content=content,
            stop_reason=str(msg.stop_reason or "end_turn"),
            usage=Usage(
                input_uncached=u.input_tokens,
                cache_write=u.cache_creation_input_tokens or 0,
                cache_read=u.cache_read_input_tokens or 0,
                output=u.output_tokens,
            ),
            dropped_thinking=dropped,
        )

    def count_tokens(self, text: str) -> int:
        try:
            r = self.client.messages.count_tokens(
                model=self.model, messages=[{"role": "user", "content": text}]
            )
            return int(r.input_tokens)
        except anthropic.APIError:
            return estimate_tokens(text)
