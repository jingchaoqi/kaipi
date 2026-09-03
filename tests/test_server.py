from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

import pytest

from kaipi import cli, server
from tests.test_cli import Echo


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Session, "_build", lambda self, model: Echo())
    s = cli.Session(tmp_path)
    canvas = server.Canvas(s, server.Hub())
    httpd = server.KaipiServer(canvas, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path: str, body: dict[str, object] | None = None) -> dict[str, object] | list[object]:
        req = urllib.request.Request(base + path, method="POST" if body is not None else "GET")
        data = json.dumps(body).encode() if body is not None else None
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Kaipi-Token", httpd.token)
        try:
            with urllib.request.urlopen(req, data) as r:
                return json.loads(r.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            return json.loads(e.read())  # type: ignore[no-any-return]

    yield canvas, call, base, httpd.token
    httpd.shutdown()
    httpd.server_close()


def _wait(canvas: server.Canvas) -> None:
    for _ in range(200):
        if not canvas.busy:
            return
        threading.Event().wait(0.01)
    raise AssertionError("turn did not finish")


def test_canvas_flow(srv) -> None:  # type: ignore[no-untyped-def]
    canvas, call, base, token = srv
    html = urllib.request.urlopen(base + "/?t=" + token).read().decode()
    assert "kaipi" in html and "/api/events" in html
    st = call("/api/state")
    assert st["nodes"] == [] and st["leaf"] is None

    q = canvas.hub.subscribe()
    assert call("/api/turn", {"text": "first"}) == {"ok": True}
    _wait(canvas)
    kinds = []
    while not q.empty():
        kinds.append(json.loads(q.get())["type"])
    assert kinds[0] == "start" and kinds[-1] == "done" and "text" in kinds

    st = call("/api/state")
    root = st["nodes"][0]["id"]
    assert st["leaf"] == root and st["trunk"] == root
    call("/api/turn", {"text": "second"})
    _wait(canvas)
    second = call("/api/state")["leaf"]

    # go back to root: trunk gets pinned so the sibling cannot steal it
    assert call("/api/go", {"id": root[-6:]}) == {"ok": True}
    st = call("/api/state")
    assert st["leaf"] == root and st["pin"] == second

    call("/api/turn", {"text": "explore"})
    _wait(canvas)
    side = call("/api/state")["leaf"]
    assert side not in (root, second)

    # graft preview + pending edges, then a turn on the trunk leaf
    assert "error" in call(f"/api/preview?src={side}&tools=")  # side is the current lineage
    call("/api/go", {"id": second})
    pv = call(f"/api/preview?src={side}&tools=")
    assert set(pv["sizes"]) == {"leaf", "leaf+summary", "branch"} and pv["on_trunk"]
    edges = [{"src_id": side, "dst_id": "", "depth": "leaf", "include_tool_outputs": []}]
    assert call("/api/pending", {"edges": edges}) == {"ok": True}
    assert call("/api/state")["pending"][0]["src_id"] == side
    call("/api/turn", {"text": "merge"})
    _wait(canvas)
    st = call("/api/state")
    assert st["pending"] == [] and st["edges"][-1]["src_id"] == side
    merged = st["leaf"]
    assert st["trunk"] == merged and st["pin"] == merged  # extending the pinned leaf moves the pin
    tr = call(f"/api/node/{merged}")
    assert tr[0]["kind"] == "graft" and tr[1]["kind"] == "user"

    # slash commands typed on the canvas go through the same verbs
    out = call("/api/command", {"line": "/tree"})["output"]
    assert "trunk burden" in out and "\x1b" not in out
    assert call("/api/archive", {"id": side}) == {"ok": True}
    assert [n["status"] for n in call("/api/state")["nodes"] if n["id"] == side] == ["archived"]
    assert call("/api/restore", {"id": side}) == {"ok": True}
    assert "error" in call("/api/go", {"id": "nope"})

    # explore from the trunk leaf: the trunk stays pinned where it is
    call("/api/turn", {"text": "side quest", "explore": True})
    _wait(canvas)
    st = call("/api/state")
    assert st["trunk"] == merged and st["pin"] == merged and st["leaf"] != merged
    assert call(f"/api/node/{st['leaf']}")[0]["text"].startswith("<kaipi:guard>")

    # rewind preview + conversation-only rewind through the API (tmp dir is not a git repo)
    rw = call(f"/api/rewind?id={merged}")
    assert rw["paths"] == [] and rw["tree"] is False
    assert call("/api/rewind", {"id": merged, "mode": "conversation"}) == {"ok": True}
    assert call("/api/state")["leaf"] == merged
    assert "error" in call("/api/rewind", {"id": merged, "mode": "code"})

    # bad ids on GET endpoints are JSON 400s, not handler crashes
    for path in ("/api/node/nope", "/api/rewind?id=nope", "/api/preview?src=nope&tools="):
        assert "error" in call(path)

    # /explore typed into the canvas runs as a streamed turn, /rewind without a mode explains
    assert call("/api/command", {"line": "/explore quick look"})["output"] == "exploration started"
    _wait(canvas)
    assert "would restore" in call("/api/command", {"line": f"/rewind {merged}"})["output"]

    # busy guard
    canvas.busy = True
    assert "error" in call("/api/turn", {"text": "x"})
    canvas.busy = False


def test_handoff_and_lock(srv, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    canvas, call, base, token = srv
    q = canvas.hub.subscribe()
    assert call("/api/command", {"line": "/cli"})["output"].startswith("handing")
    assert canvas.handoff and json.loads(q.get(timeout=1))["type"] == "handoff"

    cli.set_lock(tmp_path, "canvas", base)
    holder = cli.lock_holder(tmp_path)
    assert holder is None  # our own pid never blocks us
    (tmp_path / ".kaipi" / "lock.json").write_text(json.dumps({"pid": 2**22, "surface": "cli"}))
    assert cli.lock_holder(tmp_path) is None  # dead pid is ignored
    cli.release_lock(tmp_path)
    assert not (tmp_path / ".kaipi" / "lock.json").exists()


def test_the_api_refuses_everything_but_the_page_it_served(srv) -> None:  # type: ignore[no-untyped-def]
    """This endpoint runs bash. Three separate things must hold, and each has been a real
    vulnerability in something: a cross-origin page must not reach it, a DNS-rebound name
    must not either (which is why Origin is compared to the port we bound, never to the
    client's own Host header), and another local process must not drive it without the
    per-run token from the URL we opened."""
    canvas, call, base, token = srv
    port = base.rsplit(":", 1)[1]

    def attempt(path: str, **headers: str) -> int:
        req = urllib.request.Request(base + path, method="POST", data=b'{"text":"x"}')
        for k, v in headers.items():
            req.add_header(k.replace("_", "-"), v)
        try:
            urllib.request.urlopen(req)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    # a cross-origin page, sending the CORS "simple request" that needs no preflight
    assert attempt("/api/turn", Content_Type="text/plain", Origin="http://evil.example") == 403
    # the same page having rebound its own name to 127.0.0.1: Origin and Host agree, and an
    # implementation that compared them to each other would have let this through
    assert (
        attempt(
            "/api/turn",
            Content_Type="application/json",
            Origin=f"http://evil.example:{port}",
            Host=f"evil.example:{port}",
            X_Kaipi_Token=token,
        )
        == 403
    )
    # another local process that found the port but has no token
    assert attempt("/api/turn", Content_Type="application/json") == 403
    assert attempt("/api/reset", Content_Type="application/json") == 403
    # reads are guarded too: the transcript is not public to the machine
    req = urllib.request.Request(base + "/api/state")
    try:
        urllib.request.urlopen(req)
        raise AssertionError("/api/state must not be readable without the token")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    assert not canvas.busy, "nothing that was refused may have started a turn"

    # the page we served, carrying its token, still works
    assert "nodes" in call("/api/state")
    assert attempt("/api/turn", Content_Type="application/json", X_Kaipi_Token=token) == 200


def test_curl_cli_handoff_stops_the_server(srv) -> None:  # type: ignore[no-untyped-def]
    canvas, call, base, token = srv
    stopped: list[bool] = []
    canvas.on_handoff = lambda: stopped.append(True)
    call("/api/command", {"line": "/cli"})
    assert stopped == [True] and canvas.handoff


@pytest.mark.parametrize(
    "model", ["claude-opus-5", "gpt-5.6-terra", "gemini-3.7-flash", "deepseek-v4-flash"]
)
def test_smoke_against_the_mock_endpoint(model: str, tmp_path: Path) -> None:
    """Every wire protocol, end to end over HTTP against scripts/mockapi.py, which validates
    request shapes strictly and implements a real prefix cache. This is the closest thing to
    the Phase 2 acceptance that runs without a key: it cannot say whether the live API accepts
    these requests, but it does catch a malformed request or a broken prefix."""
    import subprocess
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "smoke.py"
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    r = subprocess.run(
        [sys.executable, str(script), "--mock", "--model", model, "--dir", str(tmp_path / model)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-3000:]
    assert "0 failed" in r.stdout


def test_smoke_script_plumbing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`scripts/smoke.py --fake` must stay green: it is the harness for the real-API run,
    so a broken script would be discovered only while spending money."""
    import subprocess
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "smoke.py"
    r = subprocess.run(
        [sys.executable, str(script), "--fake", "--dir", str(tmp_path / "repo")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "0 failed" in r.stdout
