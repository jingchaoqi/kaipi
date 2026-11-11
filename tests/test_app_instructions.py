from __future__ import annotations

from pathlib import Path

import pytest

from kaipi import core, server, summarize, titles
from kaipi.model import Message
from kaipi.providers import Reply
from kaipi.store import Log, sessions_dir
from tests.conftest import Builder
from tests.test_agent import FakeProvider, text


@pytest.mark.parametrize("instructions", [None, "", " \n\t"])
def test_default_prompt_is_unchanged(tmp_path: Path, instructions: str | None) -> None:
    (tmp_path / "AGENTS.md").write_text("PROJECT RULE")
    s = core.Session(tmp_path, new=True, app_instructions=instructions)
    assert s.log.state.app_instructions == ""
    assert s.log.state.system_prompt == (
        core.BASE_PROMPT + "\n\n# Project instructions (AGENTS.md)\nPROJECT RULE"
    )


def test_application_instructions_are_frozen_and_inherited_by_new(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("OLD PROJECT")
    s = core.Session(tmp_path, new=True, app_instructions="  APP RULE\nSECOND LINE\n")
    s.leaf = Builder(s.log).node(None, "question")
    s.save()
    old, saved = s.log.path, s.log.path.read_bytes()
    expected = (
        core.BASE_PROMPT
        + "\n\n# Application instructions\nAPP RULE\nSECOND LINE"
        + "\n\n# Project instructions (AGENTS.md)\nOLD PROJECT"
    )
    assert s.inspect_context("next").system == expected
    agents.write_text("NEW PROJECT")
    restored = core.Session(tmp_path, resume=old.stem)
    assert restored.log.state.system_prompt == expected
    assert restored.log.state.app_instructions == "APP RULE\nSECOND LINE"
    assert core.Session(tmp_path, app_instructions="APP RULE\nSECOND LINE").log.path == old
    server.Canvas(restored, server.Hub()).request_handoff()
    assert restored.log.path.read_bytes() == saved
    restored.new()
    assert restored.log.state.app_instructions == "APP RULE\nSECOND LINE"
    assert "NEW PROJECT" in restored.log.state.system_prompt
    assert "OLD PROJECT" not in restored.log.state.system_prompt
    assert old.read_bytes() == saved
    restored.resume(old.stem)
    assert restored.inspect_context("next").system == expected


@pytest.mark.parametrize("explicit", ["DIFFERENT APP", ""])
@pytest.mark.parametrize("resume", [False, True])
def test_reopening_cannot_replace_application_instructions(
    tmp_path: Path, explicit: str, resume: bool
) -> None:
    s = core.Session(tmp_path, new=True, app_instructions="ORIGINAL APP")
    s.save()
    before = s.log.path.read_bytes(), s.cursor_path.read_bytes()
    with pytest.raises(ValueError, match="已冻结"):
        core.Session(
            tmp_path, resume=s.log.path.stem if resume else None, app_instructions=explicit
        )
    assert (s.log.path.read_bytes(), s.cursor_path.read_bytes()) == before


def test_main_request_uses_application_instructions_but_metadata_prompts_do_not(
    tmp_path: Path,
) -> None:
    s = core.Session(tmp_path, new=True, app_instructions="APPLICATION RULE")

    class Capture(FakeProvider):
        def __init__(self, script: list[Reply]) -> None:
            super().__init__(script)
            self.systems: list[str] = []

        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            self.systems.append(system)
            return super().complete(system, messages, points)

    main = Capture([text("answer")])
    metadata = Capture([text('{"title": "A title"}'), text("summary")])
    s._provider, s._cheap = main, metadata
    node, _ = core.run_input(s, "question")
    summarize.ensure_summary(s.log, metadata, node, None)
    assert main.systems == [s.log.state.system_prompt]
    assert "APPLICATION RULE" in main.systems[0]
    assert metadata.systems == [titles.PROMPT, summarize.PROMPT]


@pytest.mark.parametrize("version", ["v0.1.0", "v0.1.2"])
def test_old_sessions_keep_their_original_prompt(tmp_path: Path, version: str) -> None:
    source = Path(__file__).parent / "fixtures" / f"{version}.jsonl"
    path = sessions_dir(tmp_path) / "legacy.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source.read_bytes())
    prompt = Log(path).state.system_prompt
    s = core.Session(tmp_path, resume="legacy")
    assert s.log.state.app_instructions == "" and s.log.state.system_prompt == prompt
    assert path.read_bytes() == source.read_bytes()
    s.new()
    assert s.log.state.app_instructions == "" and s.log.state.system_prompt == core.BASE_PROMPT
