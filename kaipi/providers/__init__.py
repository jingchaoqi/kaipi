"""One provider interface, two implementations (§3)."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
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


def http(base_url: str, headers: dict[str, str]) -> httpx.Client:
    """The one place an HTTP client is built. A proxy in the environment is honoured for
    real endpoints and never for loopback: a local mock, or an ollama on 127.0.0.1, is not
    something to route through the user's proxy - and doing so is exactly how `--mock` and
    `ollama` break on a machine that has one."""
    local = urlparse(base_url).hostname in ("localhost", "127.0.0.1", "::1")
    return httpx.Client(base_url=base_url, headers=headers, timeout=600, trust_env=not local)


class RateLimited(Exception):
    """The vendor said "too many requests". Every provider raises this one, so the agent
    loop owns the waiting policy rather than each client library having its own."""

    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


def _retry_after(headers: Any) -> float | None:
    try:
        return float(headers.get("retry-after") or 0) or None
    except (TypeError, ValueError):
        return None


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
    needs_key: bool = True  # a self-hosted endpoint (ollama) authenticates nobody


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
    # Moonshot Kimi: K2.x / K3. `.ai` is the international platform, `.cn` the Chinese one;
    # they are separate accounts with separate keys, so the `-cn` presets take their own.
    "kimi": _chat("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "kimi-anthropic": _msgs("https://api.moonshot.ai/anthropic", "MOONSHOT_API_KEY"),
    "kimi-cn": _chat("https://api.moonshot.cn/v1", "MOONSHOT_CN_API_KEY"),
    "kimi-anthropic-cn": _msgs("https://api.moonshot.cn/anthropic", "MOONSHOT_CN_API_KEY"),
    # GLM 5.x: Z.ai internationally, 智谱 open.bigmodel.cn in China; same split, own keys.
    "glm": _chat("https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    "glm-anthropic": _msgs("https://api.z.ai/api/anthropic", "ZAI_API_KEY"),
    "glm-cn": _chat("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    "glm-anthropic-cn": _msgs("https://open.bigmodel.cn/api/anthropic", "ZHIPU_API_KEY"),
    # OpenCode Zen (pay per token) and Go (subscription): one key, many vendors behind it.
    "opencode": _chat("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "opencode-anthropic": _msgs("https://opencode.ai/zen", "OPENCODE_API_KEY"),
    "opencode-go": _chat("https://opencode.ai/zen/go/v1", "OPENCODE_API_KEY"),
    "opencode-go-anthropic": _msgs("https://opencode.ai/zen/go", "OPENCODE_API_KEY"),
    "qwen": _chat("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "openrouter": _chat("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": _chat("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "ollama": ProviderConfig(
        api="openai-chat",
        base_url="http://localhost:11434/v1",
        api_key_env="OLLAMA_API_KEY",
        needs_key=False,
    ),
}


def auth_file() -> Path:
    """`~/.config/kaipi/auth.toml`: what `/provider` writes. Never inside a project - a key
    in a repository is a key in someone's clone."""
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "kaipi" / "auth.toml"


def auth() -> dict[str, Any]:
    p = auth_file()
    return tomllib.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save_auth(
    name: str, base_url: str = "", api_key: str = "", models: list[str] | None = None
) -> Path:
    """Adds or updates one provider. Called with only a name it changes nothing but keeps
    the file valid, so callers can save without re-asking for a key."""
    data = auth()
    entry = dict(data.setdefault("providers", {}).get(name, {}))
    for k, v in (("base_url", base_url), ("api_key", api_key)):
        if v:
            entry[k] = v
    if models:
        entry["models"] = models
    data["providers"][name] = entry
    return _write(data)


def parse_models(listed: str) -> list[str]:
    """Model ids as a person types them at a prompt: separated by commas, full-width commas
    (a Chinese input method produces those) or plain whitespace, with any spacing around
    them. Ids never contain spaces, so whitespace alone is a separator too."""
    return [m for m in re.split(r"[,\uff0c\s]+", listed) if m]


def use_model(provider: str, model: str) -> Path:
    """Activate one model, always as `<provider>/<id>`. The provider is the one the user
    listed the model under in `/provider`, with the endpoint and key they gave it; a price
    row for the same id may name a different provider (`kimi` for `kimi-k2.6`), and that
    must never win over the user's choice."""
    data = auth()
    data["model"] = f"{provider}/{model}"
    data["provider"] = provider
    return _write(data)


def _write(data: dict[str, Any]) -> Path:
    p = auth_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = [f'model = "{data.get("model", "")}"', f'provider = "{data.get("provider", "")}"', ""]
    for who, cfg in data.get("providers", {}).items():
        body += [f"[providers.{who}]"]
        for k, v in cfg.items():
            body += [f"{k} = {list(v)!r}" if isinstance(v, list) else f'{k} = "{v}"']
        body += [""]
    p.write_text("\n".join(body), encoding="utf-8")
    p.chmod(0o600)  # it holds credentials
    return p


def active_model() -> str:
    """The model spec `/model` last activated, if any."""
    return str(auth().get("model", ""))


def configured_models() -> list[tuple[str, str]]:
    """(provider, model id) for every model the user listed, across all their providers."""
    return [
        (who, m) for who, cfg in auth().get("providers", {}).items() for m in cfg.get("models", [])
    ]


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
    # Precedence: the environment (a deliberate one-off), then `/provider`'s file, then
    # the preset. So an export still overrides a saved key without having to unsave it.
    saved: dict[str, str] = auth().get("providers", {}).get(name, {})
    key = (os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "") or saved.get(
        "api_key", ""
    )
    env_base = os.environ.get(f"{name.upper().replace('-', '_')}_BASE_URL", "")
    base = (env_base or saved.get("base_url", "") or cfg.base_url).rstrip("/")
    # `api_key_env` empty means the endpoint was declared as needing no credential.
    if cfg.needs_key and cfg.api_key_env and not key:
        # A sentence the user can act on, rather than a 401 from the vendor five seconds later.
        raise ValueError(
            f"{name} 还没有 API key。运行 `kaipi provider` 配置一次，"
            f"或者 export {cfg.api_key_env}=..."
        )
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
