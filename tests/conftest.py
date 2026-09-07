from __future__ import annotations

from pathlib import Path

import pytest

from kaipi.model import (
    EdgeAdded,
    NodeCompleted,
    NodeCreated,
    ReferenceEdge,
    SessionStarted,
    Usage,
)
from kaipi.store import Log


def turn(
    text: str, answer: str, tool: tuple[str, str, str] | None = None
) -> list[dict[str, object]]:
    """A frozen payload: user text, optional (id, cmd, output) bash call, final answer."""
    msgs: list[dict[str, object]] = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    if tool:
        tid, cmd, out = tool
        msgs.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tid, "name": "bash", "input": {"command": cmd}}
                ],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tid, "content": out}],
            }
        )
    msgs.append({"role": "assistant", "content": [{"type": "text", "text": answer}]})
    return msgs


class Builder:
    def __init__(self, log: Log) -> None:
        self.log = log

    def node(
        self,
        parent: str | None,
        text: str,
        answer: str = "ok",
        tool: tuple[str, str, str] | None = None,
        edges: list[ReferenceEdge] | None = None,
        tokens: int = 100,
    ) -> str:
        nid = f"n{len(self.log.state.nodes) + 1:02d}"
        self.log.append(NodeCreated(id=nid, parent_id=parent, model="fake"))
        for e in edges or []:
            self.log.append(EdgeAdded(edge=e.model_copy(update={"dst_id": nid})))
        self.log.append(
            NodeCompleted(
                id=nid,
                payload=turn(text, answer, tool),
                usage=Usage(input_uncached=tokens, output=10),
                context_tokens=tokens,
            )
        )
        return nid


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may read - or write - the developer's real ~/.config/kaipi, which holds
    their API keys and decides which model kaipi would pick."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every endpoint in the suite is a mock on 127.0.0.1. A proxy in the developer's
    environment must not be routed through - and an `all_proxy=socks5://...` would make
    httpx raise before the request is even made."""
    for var in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.upper(), raising=False)


@pytest.fixture
def log(tmp_path: Path) -> Log:
    lg = Log(tmp_path / "s.jsonl")
    lg.append(SessionStarted(session_id="s", cwd=str(tmp_path), model="fake", system_prompt="SYS"))
    return lg


@pytest.fixture
def b(log: Log) -> Builder:
    return Builder(log)


@pytest.fixture
def tree(b: Builder) -> dict[str, str]:
    """root -> a -> a1 (trunk, deepest); root -> b (exploration with a tool call)."""
    root = b.node(None, "start", tokens=100)
    a = b.node(root, "go a", tokens=200)
    a1 = b.node(a, "go a1", tokens=300)
    bb = b.node(root, "try b", answer="b says hi", tool=("t1", "pytest", "FAILED x"), tokens=250)
    return {"root": root, "a": a, "a1": a1, "b": bb}
