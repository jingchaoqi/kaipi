"""Anthropic Messages API: fixed cache breakpoints, usage extraction, preserved-thinking guard."""

from __future__ import annotations

import copy
from typing import Any

import anthropic

from kaipi.ledger import estimate_tokens
from kaipi.model import Message, Usage
from kaipi.providers import Reply

BINDING_BETA = "thinking-binding-controls-2026-08-01"
BASH_TOOL: dict[str, str] = {"type": "bash_20250124", "name": "bash"}


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        *,
        adaptive_thinking: bool = True,
        max_tokens: int = 32_000,
        prefix_mismatch: str = "drop_block",
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
        self.max_tokens = max_tokens
        self.adaptive_thinking = adaptive_thinking
        self.prefix_mismatch = prefix_mismatch

    def _request(self, system: str, messages: list[Message], cache_points: list[int]) -> Any:
        msgs = copy.deepcopy(messages)
        for i in sorted({*cache_points, len(msgs) - 1}):  # <= 3 fixed + the moving end = 4 max
            blocks = msgs[i]["content"]
            if isinstance(blocks, list) and blocks:
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": [BASH_TOOL],
            "messages": msgs,
        }
        if self.adaptive_thinking and not self.compat:
            # Editing history is never done by kaipi; if it ever happens the API drops the
            # affected thinking blocks instead of failing, and we count them in the ledger.
            body["thinking"] = {
                "type": "adaptive",
                "block_binding": {"prefix_mismatch_behavior": self.prefix_mismatch},
            }
        return body

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        body = self._request(system, messages, cache_points)
        headers = {"anthropic-beta": BINDING_BETA} if "thinking" in body else {}
        with self.client.messages.stream(
            model=body["model"],
            max_tokens=body["max_tokens"],
            messages=body["messages"],
            extra_body={k: v for k, v in body.items() if k in ("system", "tools", "thinking")},
            extra_headers=headers,
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
