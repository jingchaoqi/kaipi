"""One provider interface, two implementations (§3)."""

from __future__ import annotations

import os
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from kaipi.model import Message, Usage

BASH_TOOL_DESCRIPTION = (
    "Run a bash command in the project directory and return combined stdout/stderr."
)
BASH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
    "additionalProperties": False,
}


class Reply(BaseModel):
    content: list[dict[str, Any]]  # raw assistant blocks: text / tool_use / thinking ...
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal
    usage: Usage = Field(default_factory=Usage)
    dropped_thinking: int = 0


class Provider(Protocol):
    model: str

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        """cache_points are indices into `messages` whose last block gets a breakpoint;
        the provider always adds one on the final message. Ignored where unsupported."""
        ...

    def count_tokens(self, text: str) -> int: ...


# --- wire protocol vs. vendor (the split pi, Codex and opencode all converge on) ---------

Api = Literal["anthropic", "openai-responses", "openai-chat", "gemini"]


class ProviderConfig(BaseModel):
    api: Api
    base_url: str = ""
    api_key_env: str = ""
    # First-party Anthropic. The `-anthropic` gateways speak the same wire protocol but
    # not the preserved-thinking beta, so the capability is a table column, not a name test.
    native_anthropic: bool = False


def _chat(url: str, env: str) -> ProviderConfig:
    return ProviderConfig(api="openai-chat", base_url=url, api_key_env=env)


def _msgs(url: str, env: str) -> ProviderConfig:
    return ProviderConfig(api="anthropic", base_url=url, api_key_env=env)


# Vendors that speak several protocols get one preset per protocol. The `-anthropic`
# variants are the endpoints these vendors document for Claude Code; kaipi's explicit
# cache breakpoints and Anthropic-shaped usage work there, preserved-thinking does not.
BUILTIN: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(
        api="anthropic", api_key_env="ANTHROPIC_API_KEY", native_anthropic=True
    ),
    "openai": ProviderConfig(
        api="openai-responses", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY"
    ),
    "gemini": ProviderConfig(
        api="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
    ),
    # DeepSeek: V4 Flash / Pro. Both protocols on the same key.
    "deepseek": _chat("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "deepseek-anthropic": _msgs("https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY"),
    # Moonshot Kimi: K2.x / K3. `.ai` is the international endpoint; set KIMI_BASE_URL for .cn.
    "kimi": _chat("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "kimi-anthropic": _msgs("https://api.moonshot.ai/anthropic", "MOONSHOT_API_KEY"),
    # Z.ai GLM: 5.x. International endpoints; set GLM_BASE_URL for open.bigmodel.cn.
    "glm": _chat("https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    "glm-anthropic": _msgs("https://api.z.ai/api/anthropic", "ZAI_API_KEY"),
    # OpenCode Zen (pay per token) and Go (subscription): one key, many vendors behind it.
    "opencode": _chat("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "opencode-anthropic": _msgs("https://opencode.ai/zen", "OPENCODE_API_KEY"),
    "opencode-go": _chat("https://opencode.ai/zen/go/v1", "OPENCODE_API_KEY"),
    "opencode-go-anthropic": _msgs("https://opencode.ai/zen/go", "OPENCODE_API_KEY"),
    "qwen": _chat("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "openrouter": _chat("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": _chat("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "ollama": _chat("http://localhost:11434/v1", "OLLAMA_API_KEY"),
}


def build(
    spec: str,
    providers: dict[str, ProviderConfig],
    *,
    provider_of: str | None = None,
    adaptive_thinking: bool = True,
) -> Provider:
    """`spec` is a model id from pricing.toml (whose entry names its provider) or
    `<provider>/<model id>` for anything not listed, e.g. `ollama/qwen3:32b`."""
    name, model = provider_of, spec
    if name is None:
        name, _, model = spec.partition("/")
        if not model:
            raise ValueError(f"{spec}: not in pricing.toml; use <provider>/<model>")
    elif model.startswith(f"{name}/"):  # a pricing entry keyed as <provider>/<model id>
        model = model[len(name) + 1 :]
    known = {**BUILTIN, **providers}
    cfg = known.get(name)
    if cfg is None:
        raise ValueError(f"unknown provider {name!r}; known: {', '.join(known)}")
    key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
    base = os.environ.get(f"{name.upper().replace('-', '_')}_BASE_URL", cfg.base_url).rstrip("/")
    if cfg.api == "anthropic":
        from kaipi.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model,
            adaptive_thinking=adaptive_thinking,
            base_url=base or None,
            api_key=key or None,
            compat=not cfg.native_anthropic,
        )
    if cfg.api == "openai-responses":
        from kaipi.providers.openai_responses import OpenAIResponsesProvider

        return OpenAIResponsesProvider(model, base_url=base, api_key=key)
    if cfg.api == "gemini":
        from kaipi.providers.gemini import GeminiProvider

        return GeminiProvider(model, base_url=base, api_key=key)
    from kaipi.providers.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(model, base_url=base, api_key=key)
