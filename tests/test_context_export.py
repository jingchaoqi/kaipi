from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from kaipi import context, context_export, core, graph, tui, views
from kaipi.model import NodeCompleted, NodeCreated, ReferenceEdge, SessionRenamed, Usage
from tests.conftest import Builder


def test_export_uses_actual_context_and_excludes_sibling_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    session = core.Session(tmp_path)
    builder = Builder(session.log)
    root = builder.node(None, "ROOT QUESTION")
    left = builder.node(root, "CURRENT BRANCH", tool=("tool1", "cat source.py", "````\nCODE\n````"))
    right = builder.node(root, "UNRELATED SECRET BRANCH")
    builder.node(right, "UNRELATED DESCENDANT")
    session.leaf = left
    session.log.append(SessionRenamed(name="检查上下文"))
    session.save()
    before = session.log.path.read_bytes(), session.cursor_path.read_bytes()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Export must not call a model")

    monkeypatch.setattr(session, "_build", forbidden)
    result = views.verb(session, ["inspect-context"])
    assert result and "打开此文件查看" in result[0]
    path = Path(result[0].splitlines()[-1])
    assert path.parent == tmp_path and path.is_absolute()
    assert re.fullmatch(r"\d{14}-检查上下文-N2-context\.md", path.name)
    document = path.read_text()
    assert "UNRELATED" not in document
    built = context.build(
        session.log.state,
        left,
        [],
        views.PLACEHOLDER,
        guard=graph.is_exploration(session.log.state, left),
    )
    assert json.loads(document) == {"system": built.system, "messages": built.messages}
    assert views.PLACEHOLDER in document
    assert (session.log.path.read_bytes(), session.cursor_path.read_bytes()) == before
    assert session.leaf == left and not session.pending
    assert path.stat().st_mode & 0o777 == 0o600
    completions = tui.SlashCompleter(session).get_completions(Document("/inspect"), CompleteEvent())
    assert [item.text for item in completions] == ["/inspect-context"]
    assert any("/inspect-context" in row for row in views.cmd_help(session))


def test_explicit_graft_preserves_request_content_without_loading_its_descendants(
    tmp_path: Path,
) -> None:
    session = core.Session(tmp_path)
    builder = Builder(session.log)
    root = builder.node(None, "root")
    left = builder.node(root, "left")
    right = builder.node(root, "EXPLICIT GRAFT")
    builder.node(right, "EXCLUDED CHILD")
    session.leaf = left
    session.pending = [ReferenceEdge(src_id=right, dst_id="", depth="leaf+summary")]
    before = session.log.path.read_bytes()
    document = context_export.export(session, views.PLACEHOLDER).read_text()
    assert "EXPLICIT GRAFT" in document and "EXCLUDED CHILD" not in document
    built = context.build(
        session.log.state,
        left,
        session.pending,
        views.PLACEHOLDER,
        guard=graph.is_exploration(session.log.state, left),
    )
    assert json.loads(document) == {"system": built.system, "messages": built.messages}
    assert len(session.pending) == 1 and session.log.path.read_bytes() == before


def test_attachments_and_nontext_blocks_are_not_dropped(tmp_path: Path) -> None:
    session = core.Session(tmp_path)
    session.log.append(NodeCreated(id="image-node", parent_id=None, model="fake"))
    payload = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at [image1]"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "preserved", "signature": "raw-signature"},
                {"type": "text", "text": "answer"},
            ],
        },
    ]
    session.log.append(NodeCompleted(id="image-node", payload=payload, usage=Usage()))
    session.leaf = "image-node"
    document = context_export.export(session, views.PLACEHOLDER).read_text()
    assert json.loads(document)["messages"][:-1] == payload


def test_empty_session_safe_names_and_same_second_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = core.Session(tmp_path)
    monkeypatch.setattr(
        context_export, "datetime", SimpleNamespace(now=lambda: datetime(2026, 11, 5, 21, 30, 45))
    )
    first = context_export.export(session, views.PLACEHOLDER)
    assert first.name == "20261105213045-未命名-root-context.md"
    first.write_text("do not overwrite")
    second = context_export.export(session, views.PLACEHOLDER)
    assert second.name == "20261105213045-未命名-root-context-2.md"
    assert first.read_text() == "do not overwrite"
    session.log.append(SessionRenamed(name='../../a/b:*?"<>|\\\n' + "很长的名字" * 50))
    third = context_export.export(session, views.PLACEHOLDER)
    assert third.parent == tmp_path and len(third.name.encode()) < 255
    assert not re.search(r'[\\/:*?"<>|\n]', third.name)
    assert (
        json.loads(third.read_text())["messages"]
        == context.build(session.log.state, None, [], views.PLACEHOLDER).messages
    )
    with pytest.raises(ValueError):
        views.verb(session, ["inspect-context", "unexpected"])
