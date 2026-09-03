#!/usr/bin/env python3
"""A strict local mock of the three wire protocols kaipi speaks, for running the smoke test
without an API key.

It is not a stand-in for the real thing, and it is deliberately built so it cannot pretend
to be. What it does do:

* **Validates requests strictly.** Anything that deviates from the documented shape is a
  400 with a reason, which surfaces as a FAIL in the smoke run rather than a silent pass.
  A malformed tool schema, a `tool_use` without its `tool_result`, a fifth `cache_control`
  breakpoint, a system prompt that changed mid-session - all rejected.
* **Implements a real prefix cache.** Requests are hashed at each `cache_control` breakpoint
  (Anthropic) or over the serialised input prefix (OpenAI / Gemini), exactly the way a real
  prefix cache keys on bytes. `cache_read_input_tokens` therefore measures whether *kaipi's*
  prefixes are actually stable across turns and shared between sibling explorations. That is
  the project's central claim and this mock can test it honestly.
* **Signs reasoning state against the conversation prefix.** A replayed thinking block whose
  prefix changed is rejected the way preserved thinking rejects it, so an accidental history
  edit fails loudly.
* **Actually solves the planted task.** A small deterministic brain reads the file, applies
  the one-line fix and runs the tests, so the agent loop is exercised over several steps.

What it cannot tell you: whether the real endpoint accepts kaipi's parameters, and what the
real cache does. Only a key answers those.

    uv run python scripts/mockapi.py --port 8900     # run standalone, for curl/debugging
    uv run python scripts/smoke.py --mock            # what you actually want
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


class Rejected(Exception):
    """A request that a real endpoint would refuse."""


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


LOOKBACK = 20  # a breakpoint walks back at most this many positions to find an entry


class PrefixCache:
    """Byte-keyed prefix cache, like the real one.

    An entry is written at each breakpoint and keys the hash of everything before it. A read
    can only land where some earlier request wrote a breakpoint, but a breakpoint walks
    backwards up to LOOKBACK positions to find one - which is what lets the first request of
    a new turn read back the previous turn, whose last request ended one position earlier."""

    def __init__(self) -> None:
        self.entries: dict[str, int] = {}  # prefix hash -> tokens it covers

    def lookup(
        self, prefixes: list[tuple[str, int]], marks: list[int] | None = None
    ) -> tuple[int, int]:
        """prefixes: (hash, cumulative tokens) for every position, in order.
        marks: positions carrying a breakpoint; None means every position (automatic caching).
        Returns (cache_read_tokens, cache_write_tokens)."""
        if not prefixes:
            return 0, 0
        at = list(range(len(prefixes))) if marks is None else sorted(set(marks))
        read = 0
        for i in at:
            for j in range(i, max(-1, i - LOOKBACK) - 1, -1):
                if prefixes[j][0] in self.entries:
                    read = max(read, prefixes[j][1])
                    break
        for i in at:
            self.entries[prefixes[i][0]] = prefixes[i][1]
        return read, max(0, prefixes[-1][1] - read)


class Signer:
    """Issues signatures bound to the conversation prefix that produced them, and refuses a
    replay whose prefix changed - the preserved-thinking check, in miniature."""

    def __init__(self) -> None:
        self.issued: dict[str, str] = {}  # signature -> prefix hash it is bound to

    def sign(self, prefix: Any) -> str:
        h = digest(prefix)
        sig = "sig_" + hashlib.sha256(f"{h}{len(self.issued)}".encode()).hexdigest()[:24]
        self.issued[sig] = h
        return sig

    def verify(self, sig: str, prefix: Any) -> None:
        if sig not in self.issued:
            raise Rejected(f"unknown signature {sig[:12]}: not issued by this endpoint")
        if self.issued[sig] != digest(prefix):
            raise Rejected(
                f"signature {sig[:12]} is bound to a different conversation "
                "(the prefix before it changed): history was edited"
            )


class Brain:
    """Deterministic agent behaviour, driven by the visible conversation. Reads before it
    writes, never writes when told the branch is read-only, and stops when it has an answer."""

    FIX = "sed -i 's/requests > 100/requests > LIMIT/' app.py"
    # the smoke repo must run anywhere, so the suite is plain asserts rather than pytest
    RUN = (
        'python -c "import test_app as t; '
        "[getattr(t, n)() for n in dir(t) if n.startswith('test_')]; print('tests pass')\""
    )
    ADD_TEST = (
        "cat >> test_app.py <<'EOF'\n\n\ndef test_at_limit() -> None:\n"
        "    assert rate_limit(LIMIT) is False\nEOF"
    )

    def intent(self, text: str) -> str:
        low = text.lower()
        if "read-only" in low or "only look" in low or "只读" in text:
            return "look"
        if "fix" in low or "修" in text:
            return "fix"
        if "add" in low and ("test" in low or "测试" in text):
            return "addtest"
        return "inspect"

    def act(self, turn: Turn) -> dict[str, Any]:
        """Returns {"command": str} to run bash, or {"text": str} to finish the turn."""
        want = self.intent(turn.last_user_text)
        ran = turn.commands_this_turn
        if not ran:
            return {"command": "cat app.py test_app.py"}
        if want == "look":
            if len(ran) == 1:
                return {"command": "grep -rn '[0-9][0-9]' --include='*.py' . | head -5"}
            return {"text": "Read-only pass: the only other literal is LIMIT = 250 in app.py."}
        if want == "fix":
            if len(ran) == 1:
                return {"command": f"{self.FIX} && {self.RUN}"}
            return {"text": "Fixed: rate_limit now compares against LIMIT. Tests pass."}
        if want == "addtest":
            if len(ran) == 1:
                return {"command": f"{self.ADD_TEST} && {self.RUN}"}
            return {"text": "Added test_at_limit for the exact boundary. Suite is green."}
        if len(ran) < 2:
            return {"command": self.RUN}
        return {"text": "The bug is the hard-coded 100 in rate_limit; it should read LIMIT."}


class Turn:
    """What the brain needs to see, extracted from whatever protocol carried it."""

    def __init__(self, last_user_text: str, commands_this_turn: list[str]) -> None:
        self.last_user_text = last_user_text
        self.commands_this_turn = commands_this_turn


class MockAPI:
    def __init__(self) -> None:
        self.cache = PrefixCache()
        self.signer = Signer()
        self.brain = Brain()
        self.systems: dict[str, str] = {}  # (model, first turn) -> that conversation's system
        self.requests: list[dict[str, Any]] = []
        self.carries_reasoning = False  # set by the protocols that have reasoning state
        self.lock = threading.Lock()

    # --- shared validation ------------------------------------------------------------

    def check_frozen_system(self, model: str, system: str, first_turn: Any = None) -> None:
        """A conversation's system prompt must not change. Keyed on (model, first message),
        so an unrelated one-shot request on the same model - a graft summary, say - is its
        own conversation and merely misses the cache, as it would on a real endpoint."""
        key = f"{model}\x00{digest(first_turn)}"
        prior = self.systems.setdefault(key, system)
        if prior != system:
            raise Rejected(
                "the system prompt changed mid-conversation; that invalidates every cached "
                "prefix and every signed thinking block"
            )

    def check_bash_tool(self, name: str, schema: dict[str, Any] | None) -> None:
        if name != "bash":
            raise Rejected(f"unexpected tool {name!r}: kaipi should only declare bash")
        if schema is not None:
            props = schema.get("properties", {})
            if "command" not in props or props["command"].get("type") != "string":
                raise Rejected("the bash tool schema must take a string `command`")

    def check_tool_pairing(self, calls: list[str], results: list[str]) -> None:
        if calls != results:
            raise Rejected(
                f"tool_use ids {calls} are not answered by matching tool_result ids {results}"
            )

    # --- Anthropic Messages -----------------------------------------------------------

    def anthropic(self, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        # NOTE: `system` below is bound by its text only, for the same reason as the markers.
        for field in ("model", "max_tokens", "messages"):
            if field not in body:
                raise Rejected(f"missing required field {field!r}")
        system = body.get("system")
        if not isinstance(system, list) or not system or system[0].get("type") != "text":
            raise Rejected("system must be a list of text blocks")
        system = [{"type": "text", "text": b["text"]} for b in system]  # drop markers
        self.check_frozen_system(str(body["model"]), system[0]["text"], body["messages"][:1])
        for t in body.get("tools", []):
            self.check_bash_tool(t.get("name", ""), t.get("input_schema"))
            if t.get("type") != "bash_20250124":
                raise Rejected("the bash tool must be declared by its Anthropic type")
            if "input_schema" in t:
                raise Rejected("bash_20250124 is schema-less; do not send input_schema")

        thinking = body.get("thinking")
        if thinking is not None:
            if thinking.get("type") not in ("adaptive", "enabled"):
                raise Rejected(f"bad thinking.type {thinking.get('type')!r}")
            if "block_binding" in thinking and "thinking-binding-controls" not in headers.get(
                "anthropic-beta", ""
            ):
                raise Rejected(
                    "thinking.block_binding requires the thinking-binding-controls beta header"
                )

        # breakpoints: at most 4, and each one keys a prefix
        msgs = body["messages"]
        marks = [
            i
            for i, m in enumerate(msgs)
            if isinstance(m.get("content"), list)
            and m["content"]
            and "cache_control" in m["content"][-1]
        ]
        n_marks = len(marks) + sum(1 for b in system if "cache_control" in b)
        if n_marks > 4:
            raise Rejected(f"{n_marks} cache_control breakpoints; the API allows 4")

        # verify every replayed thinking block against the prefix in front of it
        calls: list[str] = []
        results: list[str] = []
        bare = _strip_marks(msgs)  # markers are request options: never keyed, never bound
        for i, m in enumerate(bare):
            blocks = m.get("content")
            if not isinstance(blocks, list):
                continue
            for j, b in enumerate(blocks):
                if b.get("type") == "thinking":
                    self.signer.verify(b.get("signature", ""), [system, bare[:i], blocks[:j]])
                elif b.get("type") == "tool_use":
                    calls.append(b["id"])
                    self.check_bash_tool(b.get("name", ""), None)
                elif b.get("type") == "tool_result":
                    results.append(b["tool_use_id"])
        self.check_tool_pairing(calls, results)

        prefixes = _incremental(system, bare)
        running = prefixes[-1][1]
        read, write = self.cache.lookup(prefixes, sorted({*marks, len(msgs) - 1}))

        self.carries_reasoning = True
        turn = _turn_from_anthropic(msgs)
        action = self.brain.act(turn)
        sig = self.signer.sign([system, bare, []])
        content: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": "", "signature": sig},
        ]
        if "command" in action:
            content.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{len(self.requests):04d}",
                    "name": "bash",
                    "input": {"command": action["command"]},
                }
            )
            stop = "tool_use"
        else:
            content.append({"type": "text", "text": action["text"]})
            stop = "end_turn"
        out = tokens(json.dumps(content, ensure_ascii=False))
        return {
            "id": f"msg_{len(self.requests):04d}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": content,
            "stop_reason": stop,
            "usage": {
                "input_tokens": running - read - write,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
            },
            "input_transformations": [],
        }

    # --- OpenAI Responses -------------------------------------------------------------

    def openai_responses(self, body: dict[str, Any]) -> dict[str, Any]:
        if "input" not in body or "model" not in body:
            raise Rejected("missing model or input")
        if body.get("store") is not False:
            raise Rejected("kaipi must run stateless: store must be false")
        if "reasoning.encrypted_content" not in body.get("include", []):
            raise Rejected(
                "store:false without include:['reasoning.encrypted_content'] loses reasoning"
            )
        self.check_frozen_system(
            str(body["model"]), str(body.get("instructions", "")), body["input"][:1]
        )
        for t in body.get("tools", []):
            if t.get("type") != "function":
                raise Rejected("Responses tools must be type=function")
            self.check_bash_tool(t.get("name", ""), t.get("parameters"))
            if t.get("strict") and t["parameters"].get("additionalProperties") is not False:
                raise Rejected("strict tools need additionalProperties:false")

        items = body["input"]
        calls, results = [], []
        for i, it in enumerate(items):
            if it.get("type") == "reasoning":
                if "encrypted_content" not in it:
                    raise Rejected("a replayed reasoning item must carry encrypted_content")
                self.signer.verify(it["encrypted_content"], items[:i])
            elif it.get("type") == "function_call":
                calls.append(it["call_id"])
            elif it.get("type") == "function_call_output":
                results.append(it["call_id"])
        self.check_tool_pairing(calls, results)

        running = tokens(json.dumps([body.get("instructions", ""), items], ensure_ascii=False))
        read, _ = self.cache.lookup(_incremental(body.get("instructions"), items))
        self.carries_reasoning = True
        turn = _turn_from_openai(items)
        action = self.brain.act(turn)
        enc = self.signer.sign(items)
        output: list[dict[str, Any]] = [
            {
                "type": "reasoning",
                "id": f"rs_{len(self.requests):04d}",
                "encrypted_content": enc,
                "summary": [],
            }
        ]
        if "command" in action:
            output.append(
                {
                    "type": "function_call",
                    "call_id": f"call_{len(self.requests):04d}",
                    "name": "bash",
                    "arguments": json.dumps({"command": action["command"]}),
                }
            )
        else:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": action["text"]}],
                }
            )
        return {
            "id": f"resp_{len(self.requests):04d}",
            "status": "completed",
            "output": output,
            "usage": {
                "input_tokens": running,
                "input_tokens_details": {"cached_tokens": read},
                "output_tokens": tokens(json.dumps(output, ensure_ascii=False)),
            },
        }

    # --- Gemini generateContent -------------------------------------------------------

    def gemini(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        if "contents" not in body:
            raise Rejected("missing contents")
        sysi = body.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        self.check_frozen_system(model, sysi, body["contents"][:1])
        for tool in body.get("tools", []):
            for fn in tool.get("functionDeclarations", []):
                self.check_bash_tool(fn.get("name", ""), fn.get("parameters"))

        contents = body["contents"]
        bare = _strip_sigs(contents)  # signatures are not part of what they are bound to
        for i, c in enumerate(bare):
            if c.get("role") not in ("user", "model"):
                raise Rejected(f"bad role {c.get('role')!r}: Gemini uses user/model")
            for j, part in enumerate(contents[i].get("parts", [])):
                if "thoughtSignature" in part:
                    self.signer.verify(part["thoughtSignature"], [sysi, bare[:i], c["parts"][:j]])

        running = tokens(json.dumps([sysi, contents], ensure_ascii=False))
        read, _ = self.cache.lookup(_incremental(sysi, bare))
        self.carries_reasoning = True
        turn = _turn_from_gemini(contents)
        action = self.brain.act(turn)
        sig = self.signer.sign([sysi, bare, []])
        if "command" in action:
            parts = [
                {
                    "functionCall": {"name": "bash", "args": {"command": action["command"]}},
                    "thoughtSignature": sig,
                }
            ]
            reason = "STOP"
        else:
            parts = [{"text": action["text"], "thoughtSignature": sig}]
            reason = "STOP"
        return {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": reason}],
            "usageMetadata": {
                "promptTokenCount": running,
                "cachedContentTokenCount": read,
                "candidatesTokenCount": tokens(json.dumps(parts, ensure_ascii=False)),
                "thoughtsTokenCount": 8,
            },
        }

    def openai_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        msgs = body.get("messages")
        if not msgs or msgs[0].get("role") != "system":
            raise Rejected("chat/completions: the first message must be the system prompt")
        self.check_frozen_system(str(body.get("model")), str(msgs[0].get("content", "")), msgs[1:2])
        for t in body.get("tools", []):
            fn = t.get("function", {})
            self.check_bash_tool(fn.get("name", ""), fn.get("parameters"))
        calls, results = [], []
        for m in msgs:
            for c in m.get("tool_calls") or []:
                calls.append(c["id"])
            if m.get("role") == "tool":
                results.append(m["tool_call_id"])
        self.check_tool_pairing(calls, results)
        running = tokens(json.dumps(msgs, ensure_ascii=False))
        read, _ = self.cache.lookup(_incremental(msgs[0], msgs[1:]))
        texts: list[str] = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
        cmds = [
            json.loads(c["function"]["arguments"] or "{}").get("command", "")
            for m in msgs
            for c in (m.get("tool_calls") or [])
        ]
        since = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=0)
        n = sum(len(m.get("tool_calls") or []) for m in msgs[since:])
        action = self.brain.act(Turn(_last_user_text(texts), cmds[len(cmds) - n :] if n else []))
        if "command" in action:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{len(self.requests):04d}",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": action["command"]}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": action["text"]}
            finish = "stop"
        return {
            "id": f"chatcmpl_{len(self.requests):04d}",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": running,
                "prompt_tokens_details": {"cached_tokens": read},
                "completion_tokens": tokens(json.dumps(message, ensure_ascii=False)),
            },
        }

    def count_tokens(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"totalTokens": tokens(json.dumps(body.get("contents", []), ensure_ascii=False))}


def _incremental(head: Any, items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Prefixes at every item boundary: how an automatic prefix cache (OpenAI, Gemini and the
    OpenAI-compatible vendors) matches - longest stored prefix wins, no explicit breakpoints."""
    out: list[tuple[str, int]] = []
    running = tokens(json.dumps(head, ensure_ascii=False))
    for i, _ in enumerate(items):
        running += tokens(json.dumps(items[i], ensure_ascii=False, sort_keys=True))
        out.append((digest([head, items[: i + 1]]), running))
    return out or [(digest([head, []]), running)]


def _strip_marks(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """cache_control markers are request options, not conversation content: a real cache key
    does not include them, so neither does this one."""
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            out.append(
                {**m, "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in c]}
            )
        else:
            out.append(m)
    return out


def _strip_sigs(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **c,
            "parts": [
                {k: v for k, v in p.items() if k != "thoughtSignature"} for p in c.get("parts", [])
            ],
        }
        for c in contents
    ]


def _last_user_text(chunks: list[str]) -> str:
    return chunks[-1] if chunks else ""


def _turn_from_anthropic(msgs: list[dict[str, Any]]) -> Turn:
    texts: list[str] = []
    cmds: list[str] = []
    for m in msgs:
        c = m.get("content")
        blocks: list[dict[str, Any]] = (
            c if isinstance(c, list) else [{"type": "text", "text": str(c)}]
        )
        for b in blocks:
            if b.get("type") == "text" and m["role"] == "user":
                texts.append(str(b["text"]))
            elif b.get("type") == "tool_use":
                cmds.append(str(b.get("input", {}).get("command", "")))
    return Turn(_last_user_text(texts), _commands_this_turn(msgs, cmds))


def _commands_this_turn(msgs: list[dict[str, Any]], cmds: list[str]) -> list[str]:
    """Only the commands after the last plain user message: one node, one turn."""
    last_user = 0
    for i, m in enumerate(msgs):
        c = m.get("content")
        if (
            m.get("role") == "user"
            and isinstance(c, list)
            and any(b.get("type") == "text" for b in c)
        ):
            last_user = i
    n = sum(
        1
        for m in msgs[last_user:]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "tool_use"
    )
    return cmds[len(cmds) - n :] if n else []


def _turn_from_openai(items: list[dict[str, Any]]) -> Turn:
    texts: list[str] = []
    cmds: list[str] = []
    since = 0
    for i, it in enumerate(items):
        if it.get("role") == "user":
            content = it.get("content", [])
            parts = (
                [p.get("text", "") for p in content]
                if isinstance(content, list)
                else [str(content)]
            )
            if parts and not parts[-1].startswith("<kaipi:graft"):
                texts.append("\n".join(parts))
                since = i
        elif it.get("type") == "function_call":
            cmds.append(json.loads(it.get("arguments") or "{}").get("command", ""))
    n = sum(1 for it in items[since:] if it.get("type") == "function_call")
    return Turn(_last_user_text(texts), cmds[len(cmds) - n :] if n else [])


def _turn_from_gemini(contents: list[dict[str, Any]]) -> Turn:
    texts: list[str] = []
    cmds: list[str] = []
    since = 0
    for i, c in enumerate(contents):
        for part in c.get("parts", []):
            if "text" in part and c.get("role") == "user":
                texts.append(part["text"])
                since = i
            elif "functionCall" in part:
                cmds.append(part["functionCall"].get("args", {}).get("command", ""))
    n = sum(1 for c in contents[since:] for p in c.get("parts", []) if "functionCall" in p)
    return Turn(_last_user_text(texts), cmds[len(cmds) - n :] if n else [])


# --- SSE: the Anthropic provider streams, so the mock must too -------------------------


def sse_events(msg: dict[str, Any]) -> list[str]:
    head = {k: v for k, v in msg.items() if k != "content"} | {"content": []}
    out = [
        f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': head})}\n\n"
    ]
    for i, block in enumerate(msg["content"]):
        if block["type"] == "thinking":
            start: dict[str, Any] = {"type": "thinking", "thinking": ""}
            deltas = [
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        out.append(
            "event: content_block_start\ndata: "
            + json.dumps({"type": "content_block_start", "index": i, "content_block": start})
            + "\n\n"
        )
        for d in deltas:
            out.append(
                "event: content_block_delta\ndata: "
                + json.dumps({"type": "content_block_delta", "index": i, "delta": d})
                + "\n\n"
            )
        out.append(
            "event: content_block_stop\ndata: "
            + json.dumps({"type": "content_block_stop", "index": i})
            + "\n\n"
        )
    out.append(
        "event: message_delta\ndata: "
        + json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                "usage": msg["usage"],
            }
        )
        + "\n\n"
    )
    out.append('event: message_stop\ndata: {"type": "message_stop"}\n\n')
    return out


class Handler(BaseHTTPRequestHandler):
    server: MockServer

    def log_message(self, *_: Any) -> None:
        pass

    def _send(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        api = self.server.api
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = urlparse(self.path).path
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._send({"type": "error", "error": {"message": f"bad json: {e}"}}, 400)
            return
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            with api.lock:
                api.requests.append({"path": path, "body": body})
                if path == "/v1/messages":
                    msg = api.anthropic(body, headers)
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        for chunk in sse_events(msg):
                            self.wfile.write(chunk.encode())
                        self.wfile.flush()
                        return
                    self._send(msg)
                elif path == "/v1/messages/count_tokens":
                    self._send({"input_tokens": tokens(json.dumps(body, ensure_ascii=False))})
                elif path == "/v1/responses":
                    self._send(api.openai_responses(body))
                elif path == "/chat/completions" or path == "/v1/chat/completions":
                    self._send(api.openai_chat(body))
                elif m := re.match(r"/(?:v1beta/)?models/([^:]+):generateContent", path):
                    self._send(api.gemini(m.group(1), body))
                elif m := re.match(r"/(?:v1beta/)?models/([^:]+):countTokens", path):
                    self._send(api.count_tokens(body))
                else:
                    self._send({"error": f"no such endpoint {path}"}, 404)
        except Rejected as e:
            # shaped like both APIs' error envelopes so either client surfaces the message
            self._send(
                {"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}},
                400,
            )
        except Exception as e:  # noqa: BLE001
            self._send({"type": "error", "error": {"message": f"mock crashed: {e!r}"}}, 500)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.api = MockAPI()


def start(port: int = 0) -> tuple[MockServer, str]:
    httpd = MockServer(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()
    httpd, url = start(args.port)
    print(f"mock api on {url}  (Anthropic /v1/messages, OpenAI /v1/responses, Gemini /models/*)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
