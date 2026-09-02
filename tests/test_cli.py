from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from kaipi import cli
from kaipi.model import Message, Usage
from kaipi.providers import Reply


class Echo:
    model = "fake"

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        last = messages[-1]["content"][-1]["text"]
        return Reply(
            content=[{"type": "text", "text": f"echo:{last}"}],
            stop_reason="end_turn",
            usage=Usage(input_uncached=len(str(messages)) // 4, output=5),
        )

    def count_tokens(self, text: str) -> int:
        return len(text) // 4


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Session, "_build", lambda self, model: Echo())
    (tmp_path / "AGENTS.md").write_text("Run tests with pytest.")
    return tmp_path


def test_cli_end_to_end(repo: Path) -> None:
    r = CliRunner()
    out = r.invoke(cli.app, [], input="first task\n/tree\n/quit\n")
    assert out.exit_code == 0, out.output
    assert "echo:first task" in out.output and "[" in out.output
    s = cli.Session(repo)
    assert "AGENTS.md" in s.log.state.system_prompt and s.leaf is not None
    root = s.leaf
    # a second trunk turn (pinned, so an equally deep sibling cannot steal the trunk),
    # then go back to root and open an exploration
    assert r.invoke(cli.app, [], input="second\n/quit\n").exit_code == 0
    second = cli.Session(repo).leaf or ""
    assert r.invoke(cli.app, ["trunk", "pin", second[-6:]]).exit_code == 0
    assert r.invoke(cli.app, ["go", root[-6:]]).exit_code == 0
    out = r.invoke(cli.app, [], input="explore\n/quit\n")
    assert "exploration" in out.output
    s = cli.Session(repo)
    side = s.leaf or ""
    assert side != root
    out = r.invoke(cli.app, ["tree"])
    assert out.exit_code == 0 and "explore" in out.output and "trunk burden" in out.output
    # graft the exploration leaf onto the trunk leaf
    assert "pinned: " + second[-6:] in r.invoke(cli.app, ["trunk"]).output
    assert r.invoke(cli.app, ["go", second[-6:]]).exit_code == 0
    out = r.invoke(cli.app, ["graft", side[-6:], "--depth", "leaf"])
    assert out.exit_code == 0 and "leaf:" in out.output and "trunk burden" in out.output
    out = r.invoke(cli.app, [], input="merge\n/quit\n")
    assert out.exit_code == 0
    s = cli.Session(repo)
    assert s.log.state.edges and s.log.state.edges[-1].src_id == side
    assert "<kaipi:graft" in str(s.log.state.nodes[s.leaf or ""].payload[0])
    # archive the exploration, then restore it
    out = r.invoke(cli.app, ["archive", side[-6:]])
    assert out.exit_code == 0 and "archived 1 node" in out.output and "append-only" in out.output
    out = r.invoke(cli.app, ["ledger"])
    assert out.exit_code == 0 and "explorations" in out.output and "archived" in out.output
    assert r.invoke(cli.app, ["restore", side[-6:]]).exit_code == 0
    out = r.invoke(cli.app, ["trunk", "pin", side[-6:]])
    assert out.exit_code == 0 and "trunk pinned" in out.output
    assert "pinned: " + side[-6:] in r.invoke(cli.app, ["trunk"]).output
    assert r.invoke(cli.app, ["sessions"]).output.count("node(s)") == 1


def test_bad_node_ref(repo: Path) -> None:
    r = CliRunner()
    r.invoke(cli.app, [], input="x\n/quit\n")
    out = r.invoke(cli.app, ["go", "zzzzzz"])
    assert out.exit_code != 0
