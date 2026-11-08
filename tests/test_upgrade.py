from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kaipi import cli, context, core, providers
from kaipi.store import Log
from tests.test_cli import Echo


# Actual release-written logs; 0.1.1 shares 0.1.0's storage format.
# 0.1.2 additionally covers automatic titles and frozen image/PDF/text attachments.
@pytest.mark.parametrize("version", ["0.1.0", "0.1.2"])
def test_old_session_remains_readable_and_append_only(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    raw = (Path(__file__).parent / "fixtures" / f"v{version}.jsonl").read_bytes()
    path = tmp_path / ".kaipi/sessions/upgrade.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    cursor = tmp_path / ".kaipi/cursor.json"
    position = '{"session":"upgrade.jsonl","leaf":"n3","pending":[]}'
    cursor.write_text(position)
    auth = providers.auth_file()
    auth.parent.mkdir(parents=True)
    config = 'model = "openai/gpt-5.6-terra"\n[providers.openai]\napi_key = "synthetic"\n'
    auth.write_text(config)

    s = core.Session(tmp_path)
    assert s.leaf == "n3" and s.log.state.name == "手动标题"
    assert bool(s.log.state.titles) == (version == "0.1.2")
    completed = {
        event["id"]: event
        for line in raw.splitlines()
        if (event := json.loads(line))["type"] == "node_completed"
    }
    for nid, event in completed.items():
        node = s.log.state.nodes[nid]
        assert node.payload == event["payload"]
        assert node.usage.model_dump() == event["usage"]
        assert node.tree == event["tree"] and node.paths == event["paths"]
    prefix = completed["n1"]["payload"] + completed["n3"]["payload"]
    assert context.build(s.log.state, s.leaf, [], "继续").messages[:-1] == prefix
    assert cursor.read_text() == position and path.read_bytes() == raw

    before = s.log.state.model_dump()["nodes"]
    s._provider = Echo()
    nid, _ = cli.run_input(s, "继续", lambda kind, text: None)
    s.save()
    reopened = core.Session(tmp_path)
    assert reopened.leaf == nid
    assert path.read_bytes().startswith(raw) and auth.read_text() == config
    for old_id, node in before.items():
        assert reopened.log.state.nodes[old_id].model_dump() == node


def test_unknown_event_does_not_discard_history(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    raw = b'{"type":"session_renamed","name":"old"}\n{"type":"unknown_event"}\n'
    path.write_bytes(raw)
    with pytest.raises(ValidationError):
        Log(path)
    assert path.read_bytes() == raw
