from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kaipi import agent, context, graph, guard, summarize
from kaipi.model import Message, ReferenceEdge, Usage
from kaipi.providers import Reply
from kaipi.providers.openai_compat import from_openai, to_openai, usage_from
from kaipi.store import Log


class FakeProvider:
    """Scripted replies; records every request so tests can inspect cache points."""

    model = "fake"

    def __init__(self, script: list[Reply]) -> None:
        self.script = list(script)
        self.requests: list[tuple[list[Message], list[int]]] = []

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        self.requests.append(([dict(m) for m in messages], list(cache_points)))
        return self.script.pop(0)

    def count_tokens(self, text: str) -> int:
        return len(text) // 4


def text(t: str, stop: str = "end_turn", tokens: int = 50) -> Reply:
    return Reply(
        content=[{"type": "text", "text": t}],
        stop_reason=stop,
        usage=Usage(input_uncached=tokens, output=5),
    )


def call(tid: str, cmd: str, tokens: int = 50) -> Reply:
    return Reply(
        content=[
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": tid, "name": "bash", "input": {"command": cmd}},
        ],
        stop_reason="tool_use",
        usage=Usage(input_uncached=tokens, cache_read=10, output=5),
    )


def test_turn_with_tool_calls_is_one_node(log: Log, tmp_path: Path) -> None:
    p = FakeProvider([call("t1", "echo hi"), text("done", tokens=80)])
    nid = agent.run_turn(log, p, None, [], "say hi", exploration=False, cwd=tmp_path)
    n = log.state.nodes[nid]
    assert n.completed and n.status == "live"
    assert [m["role"] for m in n.payload] == ["user", "assistant", "user", "assistant"]
    assert "hi" in n.payload[2]["content"][0]["content"]
    assert n.payload[1]["content"][0]["type"] == "thinking"  # replayed verbatim later
    assert n.usage == Usage(input_uncached=130, cache_read=10, output=10)
    assert n.context_tokens == 80 + 5
    assert context.tool_calls(n) == [("t1", "echo hi")]
    # second request carried the whole first step
    assert len(p.requests[1][0]) == 3


def test_child_turn_replays_lineage_with_cache_points(log: Log, tmp_path: Path) -> None:
    p = FakeProvider([text("a"), text("b"), text("c")])
    root = agent.run_turn(log, p, None, [], "one", exploration=False, cwd=tmp_path)
    agent.run_turn(log, p, root, [], "two", exploration=False, cwd=tmp_path)
    agent.run_turn(log, p, root, [], "three", exploration=True, cwd=tmp_path)
    msgs, points = p.requests[2]
    assert msgs[:2] == log.state.nodes[root].payload
    assert points == [1]  # root is both the fork point and the lineage end
    assert msgs[2]["content"][0]["text"].startswith(context.GUARD_TAG)
    assert graph.fork_point(log.state, graph.trunk(log.state) or "") == root


def test_graft_edges_are_recorded_and_snapshotted(log: Log, tmp_path: Path) -> None:
    p = FakeProvider([text("a"), text("b", tokens=30), text("c", tokens=99)])
    root = agent.run_turn(log, p, None, [], "one", exploration=False, cwd=tmp_path)
    side = agent.run_turn(log, p, root, [], "side", exploration=True, cwd=tmp_path)
    e = ReferenceEdge(src_id=side, dst_id="?", depth="leaf")
    dst = agent.run_turn(log, p, root, [e], "merge", exploration=False, cwd=tmp_path)
    n = log.state.nodes[dst]
    assert log.state.edges_into(dst)[0].src_id == side
    assert n.grafts[0].src_id == side and "side" in n.grafts[0].text
    assert n.payload[0]["content"][0]["text"].startswith(context.GRAFT_TAG)


def test_truncate_keeps_head_and_tail() -> None:
    s = agent.truncate("a" * 100 + "b" * 100, limit=50)
    assert s.startswith("a" * 25) and s.endswith("b" * 25) and "omitted" in s


def test_max_steps_and_refusal(log: Log, tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    p = FakeProvider([call("t1", "true"), call("t2", "true"), call("t3", "true")])
    agent.run_turn(
        log,
        p,
        None,
        [],
        "loop",
        exploration=False,
        cwd=tmp_path,
        max_steps=2,
        hook=lambda k, t: seen.append((k, t)),
    )
    assert ("stop", "max_steps") in seen
    p2 = FakeProvider([text("no", stop="refusal")])
    agent.run_turn(
        log,
        p2,
        None,
        [],
        "x",
        exploration=False,
        cwd=tmp_path,
        hook=lambda k, t: seen.append((k, t)),
    )
    assert ("stop", "refusal") in seen


def test_snapshots_work_without_a_configured_git_identity(
    log: Log, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git commit-tree` refuses to run where no user.email is set, which would leave every
    snapshot unpinned and rewind broken. kaipi signs its own snapshots."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "nonexistent"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one")

    tree = guard.snapshot(tmp_path)
    assert tree
    guard.keep(tmp_path, "s/n", tree)
    pinned = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "refs/kaipi/s/n"], cwd=tmp_path, capture_output=True
    )
    assert pinned.returncode == 0, "the snapshot was not pinned, so gc could prune it"


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        cwd=path,
        check=True,
    )


def test_guard_warns_when_exploration_dirties_tree(log: Log, tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[str] = []
    p = FakeProvider([call("t1", "echo x > new.txt"), text("oops")])
    nid = agent.run_turn(
        log,
        p,
        None,
        [],
        "explore",
        exploration=True,
        cwd=tmp_path,
        hook=lambda k, t: seen.append(k),
    )
    assert "guard" in seen
    node = log.state.nodes[nid]
    assert guard.WARNING in str(node.payload)  # the model is told, in the turn itself
    assert node.guard_dirty  # and the fact is recorded, so no caller has to grep for it
    assert log.state.nodes[nid].paths == ["new.txt"]  # recorded even on an exploration
    guard.reset(tmp_path)
    assert not (tmp_path / "new.txt").exists()


def test_guard_silent_on_trunk(log: Log, tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[str] = []
    p = FakeProvider([call("t1", "echo x > new.txt"), text("fine")])
    nid = agent.run_turn(
        log, p, None, [], "write", exploration=False, cwd=tmp_path, hook=lambda k, t: seen.append(k)
    )
    assert "guard" not in seen
    assert not log.state.nodes[nid].guard_dirty


def test_summary_is_generated_once(log: Log, tmp_path: Path) -> None:
    p = FakeProvider([text("a"), text("b"), text("SUMMARY TEXT")])
    root = agent.run_turn(log, p, None, [], "one", exploration=False, cwd=tmp_path)
    side = agent.run_turn(log, p, root, [], "side", exploration=True, cwd=tmp_path)
    assert summarize.ensure_summary(log, p, side, root) == "SUMMARY TEXT"
    assert summarize.ensure_summary(log, p, side, root) == "SUMMARY TEXT"  # no second call
    assert p.script == []
    assert "one" not in p.requests[2][0][0]["content"][0]["text"]  # LCA excluded from path


def test_openai_conversion_roundtrip() -> None:
    msgs: list[Message] = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "tool_use", "id": "c1", "name": "bash", "input": {"command": "ls"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}],
        },
    ]
    out = to_openai("SYS", msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls"}'
    assert "thinking" not in str(out)
    content, stop = from_openai(
        {
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "c2", "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}
                ],
            },
        }
    )
    assert stop == "tool_use" and content[0]["input"] == {"command": "pwd"}
    u = usage_from(
        {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 60},
        }
    )
    assert u == Usage(input_uncached=40, cache_read=60, output=7)


def test_anthropic_request_shape() -> None:
    import os

    from kaipi.providers.anthropic import AnthropicProvider

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    p = AnthropicProvider("claude-opus-5")
    msgs: list[Message] = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        {"role": "user", "content": [{"type": "text", "text": "c"}]},
    ]
    body = p._request("SYS", msgs, [0])
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    marked = [i for i, m in enumerate(body["messages"]) if "cache_control" in m["content"][-1]]
    assert marked == [0, 2]
    assert msgs[0]["content"][0].get("cache_control") is None  # never mutates the payload
    assert body["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    assert body["tools"] == [{"type": "bash_20250124", "name": "bash"}]


def test_rewind_restores_only_agent_touched_files(log: Log, tmp_path: Path) -> None:
    from kaipi import cli

    _git_repo(tmp_path)
    (tmp_path / "mine.txt").write_text("user v1")
    p = FakeProvider(
        [
            call("t1", "printf one > a.txt"),
            text("made a"),
            call("t2", "printf two > a.txt; mkdir -p sub; printf c > sub/c.txt"),
            text("changed a, made c"),
        ]
    )
    n1 = agent.run_turn(log, p, None, [], "make a", exploration=False, cwd=tmp_path)
    (tmp_path / "mine.txt").write_text("user v2")  # a manual edit between turns
    n2 = agent.run_turn(log, p, n1, [], "change", exploration=False, cwd=tmp_path)
    st = log.state
    assert st.nodes[n1].paths == ["a.txt"] and st.nodes[n1].tree
    assert st.nodes[n2].paths == ["a.txt", "sub/c.txt"]
    assert cli.rewind_paths(st, n1) == ["a.txt", "sub/c.txt"]
    assert cli.rewind_paths(st, n2) == []
    restored = cli.rewind_code(st, tmp_path, n1)
    assert restored == ["a.txt", "sub/c.txt"]
    assert (tmp_path / "a.txt").read_text() == "one"
    assert not (tmp_path / "sub" / "c.txt").exists()
    assert (tmp_path / "mine.txt").read_text() == "user v2"  # never touched by the agent

    def ref(name: str) -> int:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "-q", name], cwd=tmp_path, capture_output=True
        ).returncode

    assert ref("refs/kaipi/undo") == 0  # pre-rewind state pinned for an undo
    assert ref(f"refs/kaipi/s/{n1}") == 0  # every node tree survives git gc (per session)
    assert (tmp_path / ".kaipi" / "index").exists()  # kaipi's private index, not the user's


def test_guard_notices_a_commit_on_an_exploration(log: Log, tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[str] = []
    p = FakeProvider(
        [
            call("t1", "git -c user.email=t@t -c user.name=t commit -q --allow-empty -m wip"),
            text("x"),
        ]
    )
    agent.run_turn(
        log,
        p,
        None,
        [],
        "explore",
        exploration=True,
        cwd=tmp_path,
        hook=lambda k, t: seen.append(k),
    )
    assert "guard" in seen  # the tree is identical, but HEAD moved


def test_rewind_from_a_subdirectory_restores_at_the_repo_root(log: Log, tmp_path: Path) -> None:
    from kaipi import cli

    _git_repo(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "f.txt").write_text("v0")
    p = FakeProvider(
        [call("t1", "printf v1 > f.txt"), text("a"), call("t2", "printf v2 > f.txt"), text("b")]
    )
    n1 = agent.run_turn(log, p, None, [], "one", exploration=False, cwd=sub)
    n2 = agent.run_turn(log, p, n1, [], "two", exploration=False, cwd=sub)
    assert log.state.nodes[n2].paths == ["sub/f.txt"]  # repo-root relative
    cli.rewind_code(log.state, sub, n1)
    assert (sub / "f.txt").read_text() == "v1"
    assert not (sub / "sub").exists()  # no stray sub/sub/f.txt


def test_restore_refuses_a_missing_snapshot(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "keep.txt").write_text("k")
    with pytest.raises(ValueError):
        guard.restore(tmp_path, "0" * 40, ["keep.txt"])
    assert (tmp_path / "keep.txt").exists()
