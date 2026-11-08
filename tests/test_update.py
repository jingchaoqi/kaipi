from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from kaipi import cli, core, tui, update, views


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[str]]:
    name = "kaipi-0.1.3-py3-none-any.whl"
    base = f"{update.REPO}/releases/download/v0.1.3/"
    data: dict[str, Any] = {
        "tag_name": "v0.1.3",
        "prerelease": False,
        "assets": [{"name": n, "browser_download_url": base + n} for n in (name, "SHA256SUMS")],
        "wheel": b"verified test wheel",
        "status": 200,
    }
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == update.API:
            return httpx.Response(
                data["status"], json={k: data[k] for k in ("tag_name", "prerelease", "assets")}
            )
        if request.url.path.endswith("SHA256SUMS"):
            checksum = data.get("checksum", hashlib.sha256(data["wheel"]).hexdigest())
            return httpx.Response(200, text=f"{checksum}  {name}\n")
        return httpx.Response(200, content=data["wheel"])

    client = httpx.Client
    monkeypatch.setattr(
        "kaipi.update.httpx.Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    monkeypatch.setattr(update, "current", lambda: "0.1.2")
    return data, requests


def test_cache_force_and_failure_backoff(github: tuple[dict[str, Any], list[str]]) -> None:
    data, requests = github
    assert update.check() == update.check() and len(requests) == 1
    update.check(force=True)
    assert len(requests) == 2
    data["status"] = 403
    with pytest.raises(httpx.HTTPStatusError):
        update.check(force=True)
    assert update.check() is None and len(requests) == 3
    data["status"] = 200
    assert update.check(force=True) is not None


def test_background_notice_once_and_opt_out(
    github: tuple[dict[str, Any], list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[threading.Thread] = []
    thread = threading.Thread

    def capture(**kwargs: Any) -> threading.Thread:
        worker = thread(**kwargs)
        threads.append(worker)
        return worker

    monkeypatch.setattr("kaipi.update.threading.Thread", capture)
    notices: list[str] = []
    update.background(notices.append)
    assert not threads  # disabled by the test environment
    monkeypatch.delenv("KAIPI_NO_UPDATE_CHECK")
    for _ in range(2):
        update.background(notices.append)
        threads[-1].join(timeout=2)
        assert not threads[-1].is_alive()
    assert len(notices) == 1
    assert all(
        text in notices[0] for text in ("0.1.2", "v0.1.3", "/releases/tag/v0.1.3", "/update")
    )


@pytest.mark.parametrize("installed", ["0.1.3", "0.1.3+g123abc", "0.1.4"])
def test_never_downgrades(
    github: tuple[dict[str, Any], list[str]], monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    monkeypatch.setattr(update, "current", lambda: installed)
    assert update.prepare(lambda text: None) is None
    assert github[1] == [update.API]


@pytest.mark.parametrize("bad", ["checksum", "url", "missing", "prerelease"])
def test_invalid_release_never_hands_off(
    github: tuple[dict[str, Any], list[str]], monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    data, _ = github
    monkeypatch.setattr(update, "installation", lambda: ("uv", Path("/tool/kaipi")))
    if bad == "prerelease":
        data["prerelease"] = True
        assert update.prepare(lambda text: None) is None
        return
    if bad == "checksum":
        data["checksum"] = "0" * 64
    elif bad == "url":
        data["assets"][0]["browser_download_url"] = "https://example.invalid/other.whl"
    else:
        data["assets"] = []
    with pytest.raises(ValueError, match="尚未替换"):
        update.prepare(lambda text: None)
    assert not list(update.cache_dir().glob("update-*"))


def test_verified_download_and_source_install_guidance(
    github: tuple[dict[str, Any], list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    with pytest.raises(ValueError, match="源码安装"):
        update.prepare(lambda text: None)
    monkeypatch.setattr(update, "installation", lambda: ("uv", tmp_path / "kaipi"))
    plan = update.prepare(lambda text: None)
    assert plan and plan.wheel.read_bytes() == github[0]["wheel"]


def test_installation_uses_own_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "tools" / "kaipi"
    prefix.mkdir(parents=True)
    executable = tmp_path / "custom bin" / "kaipi"
    executable.parent.mkdir()
    executable.touch()
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{name="kaipi", install-path=' + json.dumps(str(executable)) + "}]\n"
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr("kaipi.update.shutil.which", lambda name: "/bin/uv")
    monkeypatch.setattr(update, "__file__", str(prefix / "lib/kaipi/update.py"))
    assert update.installation() == ("/bin/uv", executable)
    monkeypatch.setattr(update, "__file__", str(tmp_path / "source/kaipi/update.py"))
    with pytest.raises(ValueError, match="uv tool"):
        update.installation()


@pytest.mark.parametrize("failure", [False, True])
def test_independent_worker_installs_and_resumes_exact_session(
    tmp_path: Path, failure: bool
) -> None:
    uv, executable = tmp_path / "fake uv", tmp_path / "fake kaipi"
    uv.write_text(f"#!{sys.executable}\nimport sys\nraise SystemExit({int(failure)})\n")
    executable.write_text(
        f"#!{sys.executable}\nimport sys,os\n"
        "print(sys.argv[1:], os.getcwd(), os.environ['UV_TOOL_DIR'])\n"
    )
    uv.chmod(0o755)
    executable.chmod(0o755)
    wheel = tmp_path / "cached wheel.whl"
    wheel.touch()
    project = tmp_path / "project with spaces"
    project.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            update.WORKER,
            str(uv),
            str(wheel),
            str(executable),
            "v0.1.3",
            "session-exact",
            str(tmp_path / "tools"),
            json.dumps(
                [
                    "正在安装 v0.1.3",
                    "更新未完成，可重试：",
                    "安装成功后继续会话：",
                    "已更新至 v0.1.3",
                    "正在接回原会话。",
                ]
            ),
        ],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == int(failure), result.stderr
    assert "session-exact" in result.stdout
    if failure:
        assert "更新未完成" in result.stdout and "可重试" in result.stdout
    else:
        assert "已更新至 v0.1.3" in result.stdout
        assert str(project) in result.stdout and "['-r', 'session-exact']" in result.stdout
    assert wheel.is_file()


def test_version_and_handoff_save_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(update, "current", lambda: "0.1.3")
    assert CliRunner().invoke(cli.app, ["version"]).output.strip() == "kaipi 0.1.3"
    assert not (tmp_path / ".kaipi").exists()
    s = core.Session(tmp_path, new=True)
    assert views.verb(s, ["version"]) == ["kaipi 0.1.3"]
    plan = update.Plan("uv", Path("/tool/kaipi"), Path("/cache/test.whl"), "v0.1.3")
    monkeypatch.setattr(update, "prepare", lambda emit: plan)

    def surface(session: core.Session) -> str:
        core.set_lock(session.cwd, "cli")
        cli.slash(session, ["update"])
        return "quit"

    def installed(p: update.Plan, *, resume: str) -> None:
        assert p == plan and resume == s.log.path.stem
        assert s.log.path.is_file()  # empty conversations must survive this handoff too
        assert not (tmp_path / ".kaipi/lock.json").exists()
        assert (
            json.loads((tmp_path / ".kaipi/cursor.json").read_text())["session"] == s.log.path.name
        )

    monkeypatch.setattr(cli, "interactive", surface)
    monkeypatch.setattr(update, "install", installed)
    cli.run_surfaces(s, "cli")


def test_queued_update_preserves_unsent_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    class Terminal:
        attachments: dict[str, object] = {}
        _pasting = False
        session = SimpleNamespace(default_buffer=SimpleNamespace(text="未发送的草稿"))

        def __init__(self, s: core.Session) -> None:
            pass

        def echo(self, line: str) -> None:
            pass

        def prompt(self, *, default: str = "") -> str:
            prompts.append(default)
            return "question" if len(prompts) == 1 else "/quit"

        def turn(self, line: str) -> list[str]:
            return ["/update"]

    monkeypatch.setattr(tui, "Terminal", Terminal)
    monkeypatch.setattr(update, "prepare", lambda emit: pytest.fail("must retain the draft"))
    assert tui.loop(core.Session(tmp_path)) == "quit"
    assert prompts == ["", "未发送的草稿"]
