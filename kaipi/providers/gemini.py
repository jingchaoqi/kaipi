"""Google Gemini generateContent (REST, API key). Thought signatures are kept on the blocks
that carried them and sent back verbatim, as thinking models require for tool use."""

from __future__ import annotations

from typing import Any

import httpx

from kaipi.ledger import estimate_tokens
from kaipi.model import Message, Usage
from kaipi.providers import BASH_TOOL_DESCRIPTION, Reply

BASH_TOOL: dict[str, Any] = {
    "functionDeclarations": [
        {
            "name": "bash",
            "description": BASH_TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }
    ]
}


def to_contents(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        blocks = (
            m["content"]
            if isinstance(m["content"], list)
            else [{"type": "text", "text": m["content"]}]
        )
        parts: list[dict[str, Any]] = []
        for b in blocks:
            t = b.get("type")
            part: dict[str, Any]
            if t == "text":
                part = {"text": b["text"]}
            elif t == "tool_use":
                part = {"functionCall": {"name": b["name"], "args": b["input"]}}
            elif t == "tool_result":
                c = b.get("content", "")
                text = c if isinstance(c, str) else "\n".join(x["text"] for x in c)
                part = {"functionResponse": {"name": "bash", "response": {"output": text}}}
            else:
                continue
            if b.get("signature"):
                part["thoughtSignature"] = b["signature"]
            parts.append(part)
        if parts:
            out.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})
    return out


def from_candidate(data: dict[str, Any], seq: int) -> tuple[list[dict[str, Any]], str]:
    cand = (data.get("candidates") or [{}])[0]
    content: list[dict[str, Any]] = []
    calls = 0
    for part in (cand.get("content") or {}).get("parts", []):
        block: dict[str, Any]
        if "functionCall" in part:
            calls += 1
            fc = part["functionCall"]
            block = {
                "type": "tool_use",
                "id": f"call_{seq}_{calls}",
                "name": fc["name"],
                "input": fc.get("args", {}),
            }
        elif "text" in part and not part.get("thought"):
            block = {"type": "text", "text": part["text"]}
        else:
            continue
        if part.get("thoughtSignature"):
            block["signature"] = part["thoughtSignature"]
        content.append(block)
    if calls:
        return content, "tool_use"
    reason = cand.get("finishReason", "STOP")
    if reason == "MAX_TOKENS":
        return content, "max_tokens"
    if reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
        return content, "refusal"
    return content, "end_turn"


def usage_from(u: dict[str, Any]) -> Usage:
    cached = int(u.get("cachedContentTokenCount", 0))
    return Usage(
        input_uncached=int(u.get("promptTokenCount", 0)) - cached,
        cache_read=cached,
        output=int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0)),
    )


class GeminiProvider:
    def __init__(
        self, model: str, *, base_url: str, api_key: str, max_tokens: int = 16_000
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.seq = 0
        self.client = httpx.Client(
            base_url=base_url, headers={"x-goog-api-key": api_key}, timeout=600
        )

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_contents(messages),
            "tools": [BASH_TOOL],
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        r = self.client.post(f"/models/{self.model}:generateContent", json=body)
        r.raise_for_status()
        data = r.json()
        self.seq += 1
        content, stop = from_candidate(data, self.seq)
        return Reply(
            content=content, stop_reason=stop, usage=usage_from(data.get("usageMetadata", {}))
        )

    def count_tokens(self, text: str) -> int:
        try:
            r = self.client.post(
                f"/models/{self.model}:countTokens",
                json={"contents": [{"role": "user", "parts": [{"text": text}]}]},
            )
            r.raise_for_status()
            return int(r.json().get("totalTokens", 0)) or estimate_tokens(text)
        except httpx.HTTPError:
            return estimate_tokens(text)
