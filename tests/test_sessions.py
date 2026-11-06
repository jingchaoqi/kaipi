from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from prompt_toolkit.application import Application, create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from kaipi import attachments, cli, context, ledger, summarize, titles, tui
from kaipi.model import Depth, Message, ReferenceEdge, SessionRenamed, SessionTitleGenerated, Usage
from kaipi.providers import Reply
from kaipi.store import Log, fold
from tests.conftest import Builder
from tests.test_cli import Echo


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cli.Session:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAIPI_MODEL", "claude-opus-5")
    monkeypatch.setenv("TERM", "xterm-256color")
    (tmp_path / "AGENTS.md").write_text("Private project instructions")
    s = cli.Session(tmp_path, new=True)
    s._provider = Echo()
    s._cheap = Echo()
    return s


def test_title_is_isolated_billed_persisted_and_only_generated_once(session: cli.Session) -> None:
    calls: list[tuple[str, list[Message], list[int]]] = []
    usage = Usage(input_uncached=27, cache_read=4, output=9)

    class Title(Echo):
        model = "claude-haiku-4-5"

        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            calls.append((system, messages, cache_points))
            return Reply(
                content=[{"type": "text", "text": '{"title": "排查登录超时"}'}],
                stop_reason="end_turn",
                usage=usage,
            )

    session._cheap = Title()
    events: list[tuple[str, str]] = []
    question = "帮我排查登录超时"
    nid, _ = cli.run_input(session, question, lambda k, t: events.append((k, t)))
    assert calls == [
        (
            titles.PROMPT,
            [{"role": "user", "content": [{"type": "text", "text": question}]}],
            [],
        )
    ]
    st = Log(session.log.path).state
    assert st.name == "排查登录超时" and len(st.nodes) == 1
    assert st.titles == [(Title.model, usage)]
    assert events.index(("text", "echo:" + question)) < events.index(("phase", "生成标题"))
    without_title = fold(
        [e for e in session.log.events if not isinstance(e, SessionTitleGenerated)]
    )
    assert context.build(st, nid, [], "继续") == context.build(without_title, nid, [], "继续")
    assert ledger.report(st, session.pricing).usage == st.nodes[nid].usage + usage
    assert ledger.total_cost(st, session.pricing) == pytest.approx(
        ledger.total_cost(without_title, session.pricing)
        + session.pricing.price(Title.model).cost(usage)
    )
    assert "排查登录超时" in cli.session_rows(session.cwd)[0][1]
    cli.cmd_rename(session, "手动名称")
    cli.run_input(session, "继续", lambda k, t: None)
    session.leaf = None  # even a new root branch must not rename this conversation
    cli.run_input(session, "另一个话题", lambda k, t: None)
    assert len(calls) == 1 and session.log.state.name == "手动名称"
    # Replay also protects explicit names if an automatic result arrives afterwards.
    replay = fold(
        [SessionRenamed(name="手动名称"), SessionTitleGenerated(name="自动", model="fake")]
    )
    assert replay.name == "手动名称" and len(replay.titles) == 1


@pytest.mark.parametrize("outcome", ["error", "empty", "tool_use", "refusal", "renamed"])
def test_title_failure_or_manual_name_never_breaks_the_answer(
    session: cli.Session, outcome: str
) -> None:
    calls = 0

    class Title(Echo):
        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            nonlocal calls
            calls += 1
            if outcome == "error":
                raise RuntimeError("endpoint unavailable")
            return Reply(
                content=[{"type": "text", "text": "" if outcome == "empty" else "not a title"}],
                stop_reason="end_turn" if outcome == "empty" else outcome,
                usage=Usage(output=3),
            )

    session._cheap = Title()
    if outcome == "renamed":
        cli.cmd_rename(session, "已经命名")
    cli.run_input(session, "question", lambda k, t: None)
    st = session.log.state
    assert st.nodes[session.leaf or ""].completed
    assert calls == (0 if outcome == "renamed" else 1)
    assert st.name == ("已经命名" if outcome == "renamed" else "")
    assert len(st.titles) == (0 if outcome in ("renamed", "error") else 1)
    if not st.name:
        assert cli.session_title(st) == "question"


def test_title_only_receives_text_not_attachment_contents(session: cli.Session) -> None:
    import base64

    class Title(Echo):
        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            assert messages == [
                {"role": "user", "content": [{"type": "text", "text": "看看 [file1]"}]}
            ]
            assert type(messages[0]["content"][0]["text"]) is str
            return Reply(
                content=[{"type": "text", "text": '{"title": "查看文件"}'}],
                stop_reason="end_turn",
                usage=Usage(output=5),
            )

    question = attachments.Input(
        "看看 [file1]",
        [
            attachments.Attachment(
                name="private.txt",
                data=base64.b64encode(b"private contents").decode(),
            )
        ],
    )
    before = session.log.path.read_bytes()
    result = titles.generate(question, Title())
    assert result.name and result.usage.output > 0
    assert session.log.path.read_bytes() == before, "generation cannot write to the session"


@pytest.mark.parametrize(
    "response",
    [
        "你好！请问有什么可以帮你的吗？",
        "Hello! How can I help you?",
        '{"title": 123}',
        '{"title": ""}',
        '{"title": "one\\ntwo"}',
        '{"title": "topic", "answer": "extra response"}',
        '{"title": "' + "x" * 41 + '"}',
    ],
)
def test_invalid_title_is_not_saved_or_displayed_on_exit(
    session: cli.Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: str,
) -> None:
    class Title(Echo):
        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            return Reply(
                content=[{"type": "text", "text": response}],
                stop_reason="end_turn",
                usage=Usage(output=7),
            )

    session._cheap = Title()
    cli.run_input(session, "你好", lambda *_: None)
    assert not session.log.state.name
    assert session.log.state.titles == [(Title.model, Usage(output=7))]
    assert cli.session_title(session.log.state) == "你好"
    assert "你好" in cli.session_rows(session.cwd)[0][1]
    monkeypatch.setattr(cli, "interactive", lambda _: "quit")
    cli.run_surfaces(session, "cli")
    output = capsys.readouterr().out
    assert "你好 |" in output and response not in output


def test_title_metadata_never_enters_history_branches_grafts_or_summaries(
    session: cli.Session,
) -> None:
    builder = Builder(session.log)
    root = builder.node(None, "opening question")
    trunk = builder.node(root, "main question")
    side = builder.node(root, "side question")
    baseline_path = session.cwd / "without-title.jsonl"
    baseline_path.write_bytes(session.log.path.read_bytes())
    baseline = Log(baseline_path)
    session.log.append(
        SessionTitleGenerated(
            name="TITLE_METADATA_MUST_NOT_REACH_MODELS",
            model="title-model",
            usage=Usage(output=3),
        )
    )
    seen: list[tuple[str, list[Message], list[int]]] = []

    class Summary(Echo):
        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            seen.append((system, messages, cache_points))
            return super().complete(system, messages, cache_points)

    for log in (baseline, session.log):
        summarize.ensure_summary(log, Summary(), side, trunk)
    assert seen[0] == seen[1], "graft summarization must not see title metadata"
    original, named = baseline.state, session.log.state
    for leaf in (None, root, trunk, side):
        for guard in (False, True):
            assert context.build(named, leaf, [], "next", guard=guard) == context.build(
                original,
                leaf,
                [],
                "next",
                guard=guard,
            )
    depths: list[Depth] = ["leaf", "leaf+summary", "branch"]
    for depth in depths:
        edge = ReferenceEdge(src_id=side, dst_id="next", depth=depth)
        assert context.build(named, trunk, [edge], "next") == context.build(
            original,
            trunk,
            [edge],
            "next",
        )
    assert named.nodes == original.nodes, "title metadata never becomes node payload"


def test_answer_is_visible_before_title_request(
    session: cli.Session, capsys: pytest.CaptureFixture[str]
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        terminal = tui.Terminal(session)
        capsys.readouterr()
        terminal.hook("text", "正文已经完成")
        assert "正文已经完成" not in capsys.readouterr().out
        terminal.hook("phase", "生成标题")
        assert "正文已经完成" in capsys.readouterr().out
        terminal.hook("ledger", "usage")
        assert "正文已经完成" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "keys,offset", [("\r", 0), ("\x1b[A\r", 1), ("\x1b[A\x1b[B\r", 0), ("\x1b", None)]
)
def test_resume_menu_highlights_scrolls_and_selects(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch, keys: str, offset: int | None
) -> None:
    paths = [session.log.path]
    for _ in range(11):
        paths.append(session._start().path)
    for i, path in enumerate(paths):
        log = Log(path)
        log.append(SessionRenamed(name=f"对话 {i} " + "很长的标题" * 30))
        os.utime(path, (1000 + i, 1000 + i))
    before = {p: p.read_bytes() for p in paths}
    visible: list[list[int]] = []
    highlight: list[bool] = []
    original_run = Application.run

    def run(app: Application[str | None]) -> str | None:
        def rendered(app: Application[str | None]) -> None:
            info = app.layout.current_window.render_info
            if info:
                visible.append(info.displayed_lines)
            screen = app.renderer.last_rendered_screen
            if screen:
                highlight.append(
                    any(
                        "radio-selected" in cell.style
                        for row in screen.data_buffer.values()
                        for cell in row.values()
                    )
                )

        app.after_render += rendered
        return original_run(app)

    monkeypatch.setattr(Application, "run", run)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        chosen = tui.pick_session(session)
    assert chosen == (paths[offset].stem if offset is not None else None)
    assert visible and 11 in visible[0]  # initially selected old session is scrolled into view
    assert all(len(lines) <= 10 for lines in visible)
    assert any(highlight)
    assert session.log.path == paths[0]
    assert {p: p.read_bytes() for p in paths} == before


def test_resume_slash_routes_to_keyboard_menu(
    session: cli.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = session.log.path
    new = session._start().path
    os.utime(old, (1000, 1000))
    os.utime(new, (1001, 1001))
    monkeypatch.delenv("KAIPI_PLAIN", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[A\r")
        assert cli.slash(session, ["resume"])
    assert session.log.path == new
