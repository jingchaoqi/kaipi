from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import typer
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
    # a configured machine: the onboarding gate is about a fresh install, not about these
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("KAIPI_MODEL", "claude-opus-5")
    (tmp_path / "AGENTS.md").write_text("Run tests with pytest.")
    return tmp_path


def test_cli_end_to_end(repo: Path) -> None:
    r = CliRunner()
    out = r.invoke(cli.app, ["-c"], input="first task\n/tree\n/quit\n")
    assert out.exit_code == 0, out.output
    assert "echo:first task" in out.output and "[" in out.output
    s = cli.Session(repo)
    assert "AGENTS.md" in s.log.state.system_prompt and s.leaf is not None
    root = s.leaf
    # a second trunk turn (pinned, so an equally deep sibling cannot steal the trunk),
    # then go back to root and open an exploration
    assert r.invoke(cli.app, ["-c"], input="second\n/quit\n").exit_code == 0
    second = cli.Session(repo).leaf or ""
    assert r.invoke(cli.app, ["trunk", "pin", second[-6:]]).exit_code == 0
    assert r.invoke(cli.app, ["go", root[-6:]]).exit_code == 0
    out = r.invoke(cli.app, ["-c"], input="explore\n/quit\n")
    assert "exploration" in out.output
    s = cli.Session(repo)
    side = s.leaf or ""
    assert side != root
    out = r.invoke(cli.app, ["tree"])
    assert out.exit_code == 0 and "explore" in out.output and "trunk burden" in out.output
    # graft the exploration leaf onto the trunk leaf
    assert "pinned: " + s.name(second) in r.invoke(cli.app, ["trunk"]).output
    assert r.invoke(cli.app, ["go", second[-6:]]).exit_code == 0
    out = r.invoke(cli.app, ["graft", side[-6:], "--depth", "leaf"])
    assert out.exit_code == 0 and "leaf:" in out.output and "trunk burden" in out.output
    out = r.invoke(cli.app, ["-c"], input="merge\n/quit\n")
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
    assert "pinned: " + s.name(side) in r.invoke(cli.app, ["trunk"]).output
    assert r.invoke(cli.app, ["sessions"]).output.count("轮") == 1


def test_bad_node_ref(repo: Path) -> None:
    r = CliRunner()
    r.invoke(cli.app, ["-c"], input="x\n/quit\n")
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
    out = r.invoke(cli.app, ["-c"], input="hello\n/canvas\n/quit\n")
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
    out = r.invoke(cli.app, ["-c"], input="one\n/explore look around\n/tree\n/quit\n")
    assert out.exit_code == 0, out.output
    s = cli.Session(repo)
    root = [n for n in s.log.state.nodes.values() if n.parent_id is None][0].id
    assert s.log.state.trunk_pin == root and s.leaf != root
    assert "exploration" in out.output


def test_rewind_slash_prompt(repo: Path) -> None:
    r = CliRunner()
    r.invoke(cli.app, ["-c"], input="one\ntwo\n/quit\n")
    s = cli.Session(repo)
    root = [n for n in s.log.state.nodes.values() if n.parent_id is None][0].id
    # not a git repo: `both` fails on the code half, `conversation` works
    out = r.invoke(cli.app, ["-c"], input=f"/rewind {root[-6:]}\nconversation\n/quit\n")
    assert out.exit_code == 0, out.output
    assert "files a code rewind would restore" in out.output
    assert cli.Session(repo).leaf == root
    out = r.invoke(cli.app, ["rewind", root[-6:], "--mode", "code"])
    assert out.exit_code != 0 and "no snapshot" in out.output


def test_stale_pin_still_protects_the_trunk(repo: Path) -> None:
    from kaipi import graph

    r = CliRunner()
    r.invoke(cli.app, ["-c"], input="one\ntwo\n/quit\n")
    s = cli.Session(repo)
    root = [n for n in s.log.state.nodes.values() if n.parent_id is None][0].id
    two = s.leaf or ""
    r.invoke(cli.app, ["trunk", "pin", two[-6:]])
    r.invoke(cli.app, ["-c"], input="three\n/quit\n")  # extends the pinned leaf: pin follows
    three = cli.Session(repo).leaf or ""
    r.invoke(cli.app, ["archive", three[-6:]])  # pin now points at an archived node
    st = cli.Session(repo).log.state
    assert st.trunk_pin == three and graph.trunk(st) == two  # stale pin, heuristic trunk
    r.invoke(cli.app, ["go", root[-6:]])  # leaving the heuristic trunk leaf must re-pin it
    st = cli.Session(repo).log.state
    assert st.trunk_pin == graph.trunk(st) == two


def test_rewind_both_refuses_archived_target(repo: Path) -> None:
    r = CliRunner()
    r.invoke(cli.app, ["-c"], input="one\ntwo\n/quit\n")
    s = cli.Session(repo)
    two = s.leaf or ""
    r.invoke(cli.app, ["archive", two[-6:]])
    out = r.invoke(cli.app, ["rewind", two[-6:], "--mode", "both"])
    assert out.exit_code != 0 and "not live" in out.output


def test_kaipi_dir_ignores_itself(repo: Path) -> None:
    """Session state lives in the user's repo, so it has to stay out of their git status."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    CliRunner().invoke(cli.app, ["-c"], input="one\n/quit\n")
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
    r.invoke(cli.app, ["-c"], input="one\n/quit\n")  # now there is one
    assert "trunk burden" in r.invoke(cli.app, ["tree"]).output


def test_the_documented_uninstall_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """README's uninstall recipe, run for real. If kaipi ever starts writing somewhere the
    recipe does not clean - a new directory, a new ref namespace - this fails."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Session, "_build", lambda self, model: Echo())
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=False
        ).stdout

    def files() -> list[str]:
        return sorted(
            str(p.relative_to(tmp_path))
            for p in tmp_path.rglob("*")
            if ".git/" not in str(p.relative_to(tmp_path)) + "/"
        )

    before = files()
    s = cli.Session(tmp_path, new=True)
    for text in ("one", "two"):
        cli.run_input(s, text, lambda k, t: None)
    s.save()
    assert (tmp_path / ".kaipi").is_dir() and git("for-each-ref", "refs/kaipi").strip()

    # ---- the three steps the README documents ----
    shutil.rmtree(tmp_path / ".kaipi")
    for ref in git("for-each-ref", "--format=%(refname)", "refs/kaipi").split():
        git("update-ref", "-d", ref)
    git("gc", "--prune=now")

    assert files() == before, "something kaipi wrote is not covered by the recipe"
    assert not git("for-each-ref", "refs/kaipi").strip(), "a pinned snapshot ref survived"
    assert not git("fsck"), "the recipe damaged the repository"
    assert "init" in git("log", "--oneline"), "history intact"
    assert not git("status", "--porcelain"), "working tree clean"


def test_switching_the_model_takes_effect_on_the_next_turn(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/model` is not a new-session affair: the next turn of this session runs on the
    model it names, the status bar says so, and the session's starting model is only a
    record."""
    built: list[str] = []

    def build(self: cli.Session, spec: str) -> Echo:
        built.append(spec)
        return Echo()

    monkeypatch.setattr(cli.Session, "_build", build)
    r = CliRunner()
    assert r.invoke(cli.app, ["-c"], input="first\n/quit\n").exit_code == 0
    assert built == ["claude-opus-5"]

    monkeypatch.setenv("KAIPI_MODEL", "kimi-anthropic-cn/kimi-k2.7-code")
    out = r.invoke(cli.app, ["-c"], input="second\n/quit\n")
    assert out.exit_code == 0, out.output
    bar = next(line for line in out.output.splitlines() if line.startswith("kaipi  "))
    assert "kimi-anthropic-cn/kimi-k2.7-code" in bar
    assert built[-1] == "kimi-anthropic-cn/kimi-k2.7-code"
    assert cli.Session(repo).log.state.model == "claude-opus-5", "the start is a record"


@pytest.mark.skipif(not hasattr(__import__("os"), "forkpty"), reason="needs a pty")
def test_the_tty_surface_keeps_input_and_status_at_the_bottom(repo: Path) -> None:
    """On a real terminal the loop is prompt_toolkit's: a prompt with the status bar under
    it. Drive it through a pty: the bar renders, a slash command runs above it, /quit ends
    it. No turn: the child is a real process that cannot see this test's fake provider."""
    import fcntl
    import os
    import select
    import struct
    import sys
    import termios
    import time

    pid, fd = os.forkpty()
    if pid == 0:  # child: become kaipi on this pty
        os.environ.setdefault("TERM", "xterm-256color")
        os.execv(sys.executable, [sys.executable, "-c", "from kaipi.cli import app; app()"])
    # a fresh pty is 0x0; the toolbar needs rows to be drawn in
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    def read_until(marker: str, timeout: float = 20.0) -> str:
        buf, end = b"", time.time() + timeout
        while marker.encode() not in buf and time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                buf += chunk
                if b"\x1b[6n" in chunk:
                    # the bar is drawn only once the cursor row is known; a real terminal
                    # answers this cursor-position request, so the test terminal does too
                    os.write(fd, b"\x1b[5;1R")
        return buf.decode(errors="replace")

    try:
        first = read_until("spent")  # the bar is drawn after the prompt line
        assert "root>" in first and "context" in first, "input above, status bar below"
        os.write(fd, b"/trunk\r")
        assert "trunk" in read_until("spent", timeout=10)
        os.write(fd, b"/quit\r")
        read_until("\x00", timeout=5)  # drain until the child closes the pty
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)


def test_help_lists_every_command(repo: Path) -> None:
    out = CliRunner().invoke(cli.app, ["-c"], input="/help\n/quit\n")
    assert out.exit_code == 0
    for name, _args, what in cli.COMMANDS:
        assert f"/{name}" in out.output and what in out.output


def test_slash_completion_offers_commands_then_node_ids(repo: Path) -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from kaipi import tui

    CliRunner().invoke(cli.app, ["-c"], input="first question\n/quit\n")
    s = cli.Session(repo)
    c = tui.SlashCompleter(s)

    def offers(text: str) -> list[tuple[str, str]]:
        return [
            (str(x.display_text), str(x.display_meta_text))
            for x in c.get_completions(Document(text), CompleteEvent())
        ]

    assert offers("hello") == []
    names = [d.split()[0] for d, _ in offers("/")]
    assert names == [f"/{n}" for n, _, _ in cli.COMMANDS]
    assert offers("/gr")[0][0].startswith("/graft <节点id>")
    ids = offers("/go ")
    assert ids and ids[0][0] == s.name(s.leaf) == "N1" and "first question" in ids[0][1]
    assert offers("/tree ") == [], "tree takes no node"


def test_every_kaipi_is_a_fresh_conversation_unless_resumed(repo: Path) -> None:
    from kaipi.store import Log, list_sessions

    r = CliRunner()
    assert r.invoke(cli.app, [], input="first\n/quit\n").exit_code == 0
    assert r.invoke(cli.app, [], input="second\n/quit\n").exit_code == 0
    a, b = list_sessions(repo)
    assert len(Log(a).state.nodes) == 1 and len(Log(b).state.nodes) == 1, "two conversations"

    # an empty fresh start leaves nothing behind
    assert r.invoke(cli.app, [], input="/quit\n").exit_code == 0
    assert r.invoke(cli.app, [], input="/quit\n").exit_code == 0
    assert [p.name for p in list_sessions(repo)] == [a.name, b.name]

    # -c picks up the latest, -r a specific one, /resume switches inside a session
    assert r.invoke(cli.app, ["-c"], input="third\n/quit\n").exit_code == 0
    assert len(Log(b).state.nodes) == 2
    out = r.invoke(cli.app, ["-r", a.stem[-6:]], input="/rename 第一条线\n/tree\n/quit\n")
    assert out.exit_code == 0, out.output
    assert Log(a).state.name == "第一条线" and "  first" in out.output, "the tree of a"
    out = r.invoke(cli.app, ["-c"], input=f"/resume {a.stem[-6:]}\nfourth\n/quit\n")
    assert out.exit_code == 0, out.output
    assert "第一条线" in out.output and len(Log(a).state.nodes) == 2
    out = r.invoke(cli.app, ["sessions"])
    assert "第一条线" in out.output and "second" in out.output
    out = r.invoke(cli.app, ["-c"], input="/resume\n2\n/quit\n")  # 1 is the current one
    assert "这个目录里的对话" in out.output and "接上" in out.output


def test_nodes_are_shown_and_addressed_by_number(repo: Path) -> None:
    """N1, N2, ... in creation order is what the tree shows and what commands take; the
    internal id still resolves by prefix or suffix, and is what the canvas API speaks."""
    r = CliRunner()
    out = r.invoke(cli.app, [], input="one\ntwo\n/tree\n/go N1\n/go 2\n/go n1\n/quit\n")
    assert out.exit_code == 0, out.output
    assert "N1>" in out.output and "N2>" in out.output and "N1 " in out.output
    s = cli.Session(repo)
    ids = list(s.log.state.nodes)
    assert s.log.state.label(ids[1]) == "N2" and s.log.state.label(None) == "root"
    assert s.resolve("N2") == ids[1] == s.resolve("2") == s.resolve(ids[1][-6:])
    assert s.leaf == ids[0], "the last /go n1 moved the cursor"
    with pytest.raises(typer.BadParameter):
        s.resolve("N9")
