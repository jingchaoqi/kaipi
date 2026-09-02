"""One provider interface, two implementations (§3)."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from kaipi.model import Message, Usage

BASH_TOOL_DESCRIPTION = (
    "Run a bash command in the project directory and return combined stdout/stderr."
)


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
