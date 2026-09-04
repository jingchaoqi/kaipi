"""OpenAI Responses API (stateless: store=false, encrypted reasoning replayed verbatim)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from kaipi.ledger import estimate_tokens
from kaipi.model import Message, Usage, blocks, text_of
from kaipi.providers import BASH_PARAMETERS, BASH_TOOL_DESCRIPTION, Reply

MAX_TOKENS = 16_000
BASH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "bash",
    "description": BASH_TOOL_DESCRIPTION,
    "strict": True,
    "parameters": BASH_PARAMETERS,
}


def to_items(messages: list[Message]) -> list[dict[str, Any]]:
    """kaipi payload -> Responses `input` items. Reasoning items are replayed unchanged so the
    model keeps its chain of thought across tool calls without server-side state."""
    items: list[dict[str, Any]] = []
    for m in messages:
        for b in blocks(m["content"]):
            t = b.get("type")
            if t == "text":
                if m["role"] == "user":
                    items.append(
                        {"role": "user", "content": [{"type": "input_text", "text": b["text"]}]}
                    )
                else:
                    items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": b["text"]}],
                        }
                    )
            elif t == "tool_use":
                items.append(
                    {
                        "type": "function_call",
                        "call_id": b["id"],
                        "name": b["name"],
                        "arguments": json.dumps(b["input"]),
                    }
                )
            elif t == "tool_result":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": b["tool_use_id"],
                        "output": text_of(b.get("content", "")),
                    }
                )
            elif t == "reasoning":
                items.append({k: v for k, v in b.items() if k != "type"} | {"type": "reasoning"})
    return items


def from_output(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    content: list[dict[str, Any]] = []
    calls = False
    for item in data.get("output", []):
        t = item.get("type")
        if t == "reasoning":
            content.append(dict(item))  # id + encrypted_content (+ summary), replayed as-is
        elif t == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    content.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "refusal":
                    content.append({"type": "text", "text": part.get("refusal", "")})
        elif t == "function_call":
            calls = True
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"command": item.get("arguments", "")}
            content.append(
                {"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": args}
            )
    if calls:
        return content, "tool_use"
    reason = (data.get("incomplete_details") or {}).get("reason")
    return content, "max_tokens" if reason == "max_output_tokens" else "end_turn"


def usage_from(u: dict[str, Any]) -> Usage:
    cached = int((u.get("input_tokens_details") or {}).get("cached_tokens", 0))
    return Usage(
        input_uncached=int(u.get("input_tokens", 0)) - cached,
        cache_read=cached,
        output=int(u.get("output_tokens", 0)),  # includes reasoning tokens (billed as output)
    )


class OpenAIResponsesProvider:
    def __init__(self, model: str, *, base_url: str, api_key: str) -> None:
        self.model = model
        self.client = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=600
        )

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        body = {
            "model": self.model,
            "instructions": system,
            "input": to_items(messages),
            "tools": [BASH_TOOL],
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": MAX_TOKENS,
        }
        r = self.client.post("/responses", json=body)
        r.raise_for_status()
        data = r.json()
        content, stop = from_output(data)
        return Reply(content=content, stop_reason=stop, usage=usage_from(data.get("usage", {})))

    def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)
