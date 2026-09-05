from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kaipi import providers
from kaipi.model import Message, Usage
from kaipi.providers import gemini, openai_responses
from kaipi.providers.gemini import GeminiProvider
from kaipi.providers.openai_compat import OpenAICompatProvider
from kaipi.providers.openai_responses import OpenAIResponsesProvider

MSGS: list[Message] = [
    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []},
            {
                "type": "tool_use",
                "id": "c1",
                "name": "bash",
                "input": {"command": "ls"},
                "signature": "SIG",
            },
        ],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
]


def _mock(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))


# --- factory -------------------------------------------------------------------------


def test_a_proxy_in_the_environment_does_not_crash_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx picks proxies up from the environment and raises at client construction -
    before any request - when it meets a socks5 proxy without socksio installed. Users do
    run behind one, so socksio ships with kaipi; nothing imports it without a proxy set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for scheme in ("socks5://127.0.0.1:1080", "http://127.0.0.1:1080"):
        monkeypatch.setenv("all_proxy", scheme)
        monkeypatch.setenv("https_proxy", scheme)
        for name, model in (
            ("anthropic", "claude-opus-5"),
            ("deepseek", "deepseek-v4-flash"),
            ("openai", "gpt-5.6-terra"),
            ("gemini", "gemini-3.7-flash"),
        ):
            providers.build(model, {}, provider_of=name)


def test_loopback_is_never_sent_through_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy is for the internet. Routing a local mock or an ollama on 127.0.0.1 through
    it is how those break on a machine that has one - and the user should not have to know
    that, so kaipi decides it rather than asking for a no_proxy."""
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1080")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1080")
    assert not providers.http("http://127.0.0.1:8123/v1", {})._mounts
    assert not providers.http("http://localhost:11434/v1", {})._mounts
    assert providers.http("https://api.moonshot.cn/v1", {})._mounts, "a real endpoint still uses it"


def test_factory_picks_wire_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY", "k2")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k3")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k4")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://box:11434/v1")
    assert isinstance(
        providers.build("gpt-5.6-terra", {}, provider_of="openai"), OpenAIResponsesProvider
    )
    assert isinstance(providers.build("gemini-3.7-flash", {}, provider_of="gemini"), GeminiProvider)
    p = providers.build("deepseek-v4-flash", {}, provider_of="deepseek")
    assert isinstance(p, OpenAICompatProvider)
    assert str(p.client.base_url).startswith("https://api.deepseek.com")
    assert p.client.headers["authorization"] == "Bearer k3"
    # <provider>/<model> for anything not in pricing.toml; env overrides the base url
    q = providers.build("ollama/qwen3:32b", {})
    assert isinstance(q, OpenAICompatProvider) and q.model == "qwen3:32b"
    assert str(q.client.base_url).startswith("http://box:11434")
    custom = {"vllm": providers.ProviderConfig(api="openai-chat", base_url="http://l:8000/v1")}
    assert providers.build("vllm/llama", custom).model == "llama"
    with pytest.raises(ValueError):
        providers.build("nope", {})
    with pytest.raises(ValueError):
        providers.build("nope/m", {})
    from kaipi.providers.anthropic import AnthropicProvider

    assert isinstance(
        providers.build("claude-opus-5", {}, provider_of="anthropic"), AnthropicProvider
    )


# --- OpenAI Responses --------------------------------------------------------------------


def test_responses_items_roundtrip() -> None:
    items = openai_responses.to_items(MSGS)
    assert [i.get("type") or i["role"] for i in items] == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
        "assistant",
    ]
    assert items[1] == {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "ENC",
        "summary": [],
    }
    assert items[2]["call_id"] == "c1" and json.loads(items[2]["arguments"]) == {"command": "ls"}
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "a.py"}


def test_responses_end_to_end() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "reasoning", "id": "rs_9", "encrypted_content": "E9", "summary": []},
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "bash",
                        "arguments": '{"command":"pwd"}',
                    },
                ],
                "status": "completed",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "input_tokens_details": {"cached_tokens": 100},
                },
            },
        )

    p = OpenAIResponsesProvider("gpt-x", base_url="http://x", api_key="k")
    p.client = _mock(handler)
    r = p.complete("SYS", MSGS[:1], [])
    body = seen[0]
    assert body["instructions"] == "SYS" and body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["tools"][0]["name"] == "bash" and body["tools"][0]["strict"] is True
    assert r.stop_reason == "tool_use"
    assert r.content[0]["type"] == "reasoning" and r.content[0]["encrypted_content"] == "E9"
    assert r.content[1] == {
        "type": "tool_use",
        "id": "call_9",
        "name": "bash",
        "input": {"command": "pwd"},
    }
    assert r.usage == Usage(input_uncached=20, cache_read=100, output=30)
    # replaying the reply keeps the reasoning item verbatim
    replay = openai_responses.to_items([{"role": "assistant", "content": r.content}])
    assert replay[0] == {
        "type": "reasoning",
        "id": "rs_9",
        "encrypted_content": "E9",
        "summary": [],
    }


def test_responses_stop_reasons() -> None:
    content, stop = openai_responses.from_output(
        {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
            "incomplete_details": {"reason": "max_output_tokens"},
        }
    )
    assert content == [{"type": "text", "text": "hi"}] and stop == "max_tokens"


# --- Gemini --------------------------------------------------------------------------


def test_gemini_contents_keep_thought_signatures() -> None:
    c = gemini.to_contents(MSGS)
    assert [x["role"] for x in c] == ["user", "model", "user", "model"]
    assert c[1]["parts"] == [
        {"functionCall": {"name": "bash", "args": {"command": "ls"}}, "thoughtSignature": "SIG"}
    ]
    assert c[2]["parts"] == [{"functionResponse": {"name": "bash", "response": {"output": "a.py"}}}]


def test_gemini_end_to_end() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content)))
        if req.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 7})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "thinking...", "thought": True},
                                {
                                    "functionCall": {"name": "bash", "args": {"command": "ls"}},
                                    "thoughtSignature": "TS",
                                },
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 50,
                    "cachedContentTokenCount": 40,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 12,
                },
            },
        )

    p = GeminiProvider("gemini-x", base_url="http://x", api_key="k")
    p.client = _mock(handler)
    r = p.complete("SYS", MSGS[:1], [])
    path, body = seen[0]
    assert path == "/models/gemini-x:generateContent"
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "bash"
    assert r.stop_reason == "tool_use"
    assert r.content == [
        {
            "type": "tool_use",
            "id": "call_1_1",
            "name": "bash",
            "input": {"command": "ls"},
            "signature": "TS",
        }
    ]
    assert r.usage == Usage(input_uncached=10, cache_read=40, output=17)
    assert p.count_tokens("abc") == 7


def test_gemini_finish_reasons() -> None:
    assert (
        gemini.from_candidate(
            {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "MAX_TOKENS"}]},
            1,
        )[1]
        == "max_tokens"
    )
    assert (
        gemini.from_candidate(
            {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}, 1
        )[1]
        == "refusal"
    )
    assert gemini.from_candidate(
        {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}, 1
    ) == ([{"type": "text", "text": "ok"}], "end_turn")


def test_vendor_presets_in_both_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaipi.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("MOONSHOT_API_KEY", "km")
    monkeypatch.setenv("ZAI_API_KEY", "kz")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "kd")
    monkeypatch.setenv("OPENCODE_API_KEY", "ko")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    kimi = providers.build("kimi-k3", {}, provider_of="kimi")
    assert isinstance(kimi, OpenAICompatProvider)
    assert str(kimi.client.base_url).startswith("https://api.moonshot.ai/v1")
    k3 = providers.build("kimi-anthropic/kimi-k3[1m]", {})
    assert isinstance(k3, AnthropicProvider) and k3.compat and k3.model == "kimi-k3[1m]"
    assert str(k3.client.base_url).startswith("https://api.moonshot.ai/anthropic")
    assert k3.client.api_key == "km" and k3.client.auth_token == "km"
    glm = providers.build("glm-anthropic/glm-5.2[1m]", {})
    assert str(glm.client.base_url).startswith("https://api.z.ai/api/anthropic")  # type: ignore[attr-defined]
    ds = providers.build("deepseek-anthropic/deepseek-v4-pro", {})
    assert str(ds.client.base_url).startswith("https://api.deepseek.com/anthropic")  # type: ignore[attr-defined]
    go = providers.build("opencode-go/kimi-k3", {}, provider_of="opencode-go")
    assert isinstance(go, OpenAICompatProvider) and go.model == "kimi-k3"  # prefix stripped
    assert str(go.client.base_url).startswith("https://opencode.ai/zen/go/v1")
    goa = providers.build("opencode-go-anthropic/claude-opus-5", {})
    assert str(goa.client.base_url).startswith("https://opencode.ai/zen/go")  # type: ignore[attr-defined]
    monkeypatch.setenv("KIMI_ANTHROPIC_BASE_URL", "https://api.moonshot.cn/anthropic")
    cn = providers.build("kimi-anthropic/kimi-k3[1m]", {})
    assert str(cn.client.base_url).startswith("https://api.moonshot.cn/anthropic")  # type: ignore[attr-defined]


def test_compat_gateways_get_no_preserved_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaipi.providers.anthropic import AnthropicProvider

    native = AnthropicProvider("claude-opus-5", api_key="k")
    gw = AnthropicProvider(
        "kimi-k3[1m]", base_url="https://api.moonshot.ai/anthropic", api_key="k", compat=True
    )
    msgs: list[Message] = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    assert "thinking" in native._request("S", msgs, [])
    body = gw._request("S", msgs, [])
    assert "thinking" not in body
    assert body["messages"][0]["content"][-1]["cache_control"] == {
        "type": "ephemeral"
    }  # breakpoints stay
