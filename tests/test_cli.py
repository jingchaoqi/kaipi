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


def test_handoff_between_surfaces(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kaipi import server

    calls: list[str] = []

    def fake_serve(s: cli.Session, port: int = 0, *, open_browser: bool = True) -> str:
        calls.append("canvas")
        return "http://127.0.0.1:1/"

    monkeypatch.setattr(server, "serve", fake_serve)
    r = CliRunner()
    # CLI -> /canvas hands off (prompt closes) -> canvas returns -> CLI prompt again -> /quit
    out = r.invoke(cli.app, [], input="hello\n/canvas\n/quit\n")
    assert out.exit_code == 0, out.output
    assert calls == ["canvas"]
    assert "canvas opened in your browser" in out.output
    assert "back in this terminal" in out.output
    assert not (repo / ".kaipi" / "lock.json").exists()  # released on exit
    # `kaipi canvas` starts on the canvas, then drops into the CLI when it comes back
    out = r.invoke(cli.app, ["canvas"], input="/quit\n")
    assert out.exit_code == 0 and calls == ["canvas", "canvas"]


def test_refuses_when_open_elsewhere(repo: Path) -> None:
    import json
    import os

    (repo / ".kaipi").mkdir(exist_ok=True)
    (repo / ".kaipi" / "lock.json").write_text(
        json.dumps({"pid": os.getppid(), "surface": "canvas", "url": "http://127.0.0.1:9/"})
    )
    try:
        out = CliRunner().invoke(cli.app, ["go", "x"])
        assert out.exit_code == 1 and "open in the canvas" in out.output
        assert CliRunner().invoke(cli.app, ["tree"]).exit_code == 0  # read-only still fine
    finally:
        cli.release_lock(repo)


def test_explore_from_trunk_leaf(repo: Path) -> None:
    r = CliRunner()
    out = r.invoke(cli.app, [], input="one\n/explore look around\n/tree\n/quit\n")
    assert out.exit_code == 0, out.output
    s = cli.Session(repo)
    root = [n for n in s.log.state.nodes.values() if n.parent_id is None][0].id
    assert s.log.state.trunk_pin == root and s.leaf != root
    assert "exploration" in out.output


def test_rewind_slash_prompt(repo: Path) -> None:
    r = CliRunner()
    r.invoke(cli.app, [], input="one\ntwo\n/quit\n")
    s = cli.Session(repo)
    root = [n for n in s.log.state.nodes.values() if n.parent_id is None][0].id
    # not a git repo: `both` fails on the code half, `conversation` works
    out = r.invoke(cli.app, [], input=f"/rewind {root[-6:]}\nconversation\n/quit\n")
    assert out.exit_code == 0, out.output
    assert "files a code rewind would restore" in out.output
    assert cli.Session(repo).leaf == root
    out = r.invoke(cli.app, ["rewind", root[-6:], "--mode", "code"])
    assert out.exit_code != 0 and "no snapshot" in out.output


def test_stale_pin_still_protects_the_trunk(repo: Path) -> None:
    from kaipi import graph

    r = CliRunner()
    r.invoke(cli.app, [], input="one\ntwo\n/quit\n")
    s = cli.Session(repo)
    root = [n for n in s.log.state.nodes.values() if n.parent_id is None][0].id
    two = s.leaf or ""
    r.invoke(cli.app, ["trunk", "pin", two[-6:]])
    r.invoke(cli.app, [], input="three\n/quit\n")  # extends the pinned leaf: pin follows
    three = cli.Session(repo).leaf or ""
    r.invoke(cli.app, ["archive", three[-6:]])  # pin now points at an archived node
    st = cli.Session(repo).log.state
    assert st.trunk_pin == three and graph.trunk(st) == two  # stale pin, heuristic trunk
    r.invoke(cli.app, ["go", root[-6:]])  # leaving the heuristic trunk leaf must re-pin it
    st = cli.Session(repo).log.state
    assert st.trunk_pin == graph.trunk(st) == two


def test_rewind_both_refuses_archived_target(repo: Path) -> None:
    r = CliRunner()
    r.invoke(cli.app, [], input="one\ntwo\n/quit\n")
    s = cli.Session(repo)
    two = s.leaf or ""
    r.invoke(cli.app, ["archive", two[-6:]])
    out = r.invoke(cli.app, ["rewind", two[-6:], "--mode", "both"])
    assert out.exit_code != 0 and "not live" in out.output


def test_kaipi_dir_ignores_itself(repo: Path) -> None:
    """Session state lives in the user's repo, so it has to stay out of their git status."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    CliRunner().invoke(cli.app, [], input="one\n/quit\n")
    assert (repo / ".kaipi" / ".gitignore").read_text() == "*\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert ".kaipi" not in status, f"kaipi dirtied the user's working tree: {status}"


def test_read_only_commands_do_not_create_a_session(repo: Path) -> None:
    r = CliRunner()
    for cmd in (["tree"], ["ledger"], ["trunk"]):
        out = r.invoke(cli.app, cmd)
        assert out.exit_code == 0 and "no session in this directory yet" in out.output
    assert not (repo / ".kaipi" / "sessions").exists()
    assert r.invoke(cli.app, ["sessions"]).output == ""
    r.invoke(cli.app, [], input="one\n/quit\n")  # now there is one
    assert "trunk burden" in r.invoke(cli.app, ["tree"]).output
