"""Any OpenAI-compatible /chat/completions endpoint. Converts kaipi's Anthropic-shaped
payloads at the boundary; thinking blocks are dropped; cached tokens read from usage."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from kaipi.ledger import estimate_tokens
from kaipi.model import Message, Usage
from kaipi.providers import BASH_TOOL_DESCRIPTION, Reply

BASH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": BASH_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


def to_openai(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        blocks = (
            m["content"]
            if isinstance(m["content"], list)
            else [{"type": "text", "text": m["content"]}]
        )
        if m["role"] == "assistant":
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        texts: list[str] = []
        for b in blocks:
            if b.get("type") == "tool_result":
                c = b.get("content", "")
                body = c if isinstance(c, str) else "\n".join(x["text"] for x in c)
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": body})
            elif b.get("type") == "text":
                texts.append(b["text"])
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def from_openai(choice: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    msg = choice["message"]
    content: list[dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {"command": call["function"]["arguments"]}
        content.append(
            {"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args}
        )
    stop = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(
        choice.get("finish_reason") or "stop", "end_turn"
    )
    return content, stop


def usage_from(u: dict[str, Any]) -> Usage:
    prompt = int(u.get("prompt_tokens", 0))
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    cached = cached or int(u.get("prompt_cache_hit_tokens", 0))  # DeepSeek naming
    return Usage(
        input_uncached=prompt - cached,
        cache_write=0,
        cache_read=cached,
        output=int(u.get("completion_tokens", 0)),
    )


class OpenAICompatProvider:
    def __init__(self, model: str, *, max_tokens: int = 16_000) -> None:
        self.model = model
        self.max_tokens = max_tokens
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        key = os.environ.get("OPENAI_API_KEY", "")
        self.client = httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=600
        )

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        body = {
            "model": self.model,
            "messages": to_openai(system, messages),
            "tools": [BASH_TOOL],
            "max_completion_tokens": self.max_tokens,
        }
        r = self.client.post("/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        content, stop = from_openai(data["choices"][0])
        return Reply(content=content, stop_reason=stop, usage=usage_from(data.get("usage", {})))

    def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)
