from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from kaipi import attachments, core, server
from kaipi.model import Message, ReferenceEdge, Usage
from kaipi.providers import Reply
from tests.test_cli import Echo


class SlowEcho(Echo):
    """Answers only after a while, and asks for a tool call first, so a stop has somewhere
    to land: the real interruption points are the step boundary and the running command."""

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        time.sleep(0.15)
        if len(messages) < 6:
            return Reply(
                content=[
                    {
                        "type": "tool_use",
                        "id": f"t{len(messages)}",
                        "name": "bash",
                        "input": {"command": "sleep 5"},
                    }
                ],
                stop_reason="tool_use",
                usage=Usage(input_uncached=20, output=5),
            )
        return super().complete(system, messages, cache_points)


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core.Session, "_build", lambda self, model: Echo())
    s = core.Session(tmp_path)
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


def test_retry_http_and_slash_share_the_core(srv) -> None:  # type: ignore[no-untyped-def]
    canvas, call, _, _ = srv
    assert "error" in call("/api/retry", {})
    assert call("/api/turn", {"text": "original question"}) == {"ok": True}
    _wait(canvas)
    old = canvas.s.leaf
    assert call("/api/state")["retryable"] is True
    assert call("/api/retry", {}) == {"ok": True}
    _wait(canvas)
    new = canvas.s.leaf
    assert new != old and canvas.s.log.state.nodes[new].parent_id is None
    assert "error" in call("/api/retry", {"id": old})
    assert call("/api/command", {"line": "/retry"}) == {"output": ""}
    _wait(canvas)
    assert len(canvas.s.log.state.nodes) == 3 and canvas.s.leaf != new
    canvas.s.claim()
    try:
        assert "error" in call("/api/retry", {})
    finally:
        canvas.s.idle.set()
    canvas.request_handoff()
    assert "error" in call("/api/retry", {})
    assert len(canvas.s.log.state.nodes) == 3


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
    assert "主干上下文" in out and "\x1b" not in out
    assert "\x1b" not in call("/api/command", {"line": "/ledger"})["output"]
    before = canvas.s.log.path.read_bytes()
    status = call("/api/command", {"line": "/status"})["output"]
    assert "会话费用" in status and "上下文" in status and "\x1b" not in status
    assert canvas.s.log.path.read_bytes() == before
    # a verb that would ask on stdin must not wait there with the lock held
    assert "需要在终端执行" in call("/api/command", {"line": "/provider"})["output"]
    assert "未知命令" in call("/api/command", {"line": "/nope"})["output"]
    assert call("/api/command", {"line": "/go nope"})["output"].startswith("错误：")
    assert "用法：/rewind <节点id>" in call("/api/command", {"line": "/rewind"})["output"]
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
    assert call("/api/command", {"line": "/explore quick look"})["output"] == "已开始探索"
    _wait(canvas)
    assert "将恢复这些文件" in call("/api/command", {"line": f"/rewind {merged}"})["output"]

    # busy guard
    canvas.s.claim()
    assert "error" in call("/api/turn", {"text": "x"})
    assert "error" in call("/api/handoff", {}) and not canvas.handoff
    assert "error" in call("/api/pending", {"edges": []})
    canvas.s.idle.set()


def test_new_on_canvas_preserves_history_and_rejects_switching_while_busy(srv) -> None:  # type: ignore[no-untyped-def]
    canvas, call, _, _ = srv
    call("/api/turn", {"text": "old question"})
    _wait(canvas)
    previous = canvas.s.log.path
    payload = previous.read_bytes()
    canvas.s.pending = [ReferenceEdge(src_id=canvas.s.leaf or "", dst_id="")]
    canvas.s.claim()
    assert "error" in call("/api/command", {"line": "/new"})
    canvas.s.idle.set()
    assert canvas.s.log.path == previous
    assert call("/api/command", {"line": "/new nope"})["output"].startswith("错误：")
    assert canvas.s.log.path == previous

    assert "已开始新会话" in call("/api/command", {"line": "/new"})["output"]
    state = call("/api/state")
    assert state["session"] != previous.stem and not state["nodes"]
    assert state["leaf"] is None and not state["pending"] and state["cost"] == 0
    assert previous.read_bytes() == payload
    call("/api/turn", {"text": "new question"})
    _wait(canvas)
    node = next(iter(canvas.s.log.state.nodes.values()))
    assert node.parent_id is None and "old question" not in str(node.payload)
    call("/api/command", {"line": f"/resume {previous.stem}"})
    assert call("/api/state")["session"] == previous.stem


def test_handoff_and_lock(srv, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    canvas, call, base, token = srv
    q = canvas.hub.subscribe()
    assert call("/api/command", {"line": "/cli"})["output"].startswith("正在将会话")
    assert canvas.handoff and json.loads(q.get(timeout=1))["type"] == "handoff"

    core.set_lock(tmp_path, "canvas", base)
    holder = core.lock_holder(tmp_path)
    assert holder is None  # our own pid never blocks us
    (tmp_path / ".kaipi" / "lock.json").write_text(json.dumps({"pid": 2**22, "surface": "cli"}))
    assert core.lock_holder(tmp_path) is None  # dead pid is ignored
    core.release_lock(tmp_path)
    assert not (tmp_path / ".kaipi" / "lock.json").exists()


def test_stopping_a_turn_from_the_canvas(srv) -> None:  # type: ignore[no-untyped-def]
    """The stop button is accepted while a turn is running - it is the only thing that is -
    and what comes back is an aborted node the canvas can show as discarded."""
    canvas, call, base, token = srv
    canvas.s._provider = SlowEcho()
    q = canvas.hub.subscribe()
    assert call("/api/turn", {"text": "something long"}) == {"ok": True}
    for _ in range(200):  # let the turn get going
        if canvas.busy:
            break
        threading.Event().wait(0.01)
    assert call("/api/pin", {"id": "x"})  # a normal verb is refused while busy
    assert call("/api/stop", {}) == {"ok": True}
    _wait(canvas)

    events = []
    while not q.empty():
        events.append(json.loads(q.get_nowait()))
    aborted = [e for e in events if e["type"] == "aborted"]
    assert aborted, f"the canvas is told the turn was stopped: {[e['type'] for e in events]}"
    st = call("/api/state")
    nodes = st["nodes"]
    assert isinstance(nodes, list) and nodes
    dead = [n for n in nodes if n["status"] == "aborted"]
    assert len(dead) == 1, "the discarded turn is visible on the canvas, marked aborted"
    assert st["leaf"] == dead[0]["parent_id"], "the cursor moved back to the parent"
    assert "saved" in st["status"] and "interrupt" in st["status"]["saved"]


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


def test_closed_canvas_rejects_all_writes_including_stop(srv) -> None:  # type: ignore[no-untyped-def]
    canvas, call, base, token = srv
    assert call("/api/handoff", {}) == {"ok": True}
    before = canvas.s.log.path.read_bytes(), canvas.s.cursor_path.read_bytes()
    canvas.s.claim()  # the CLI has taken over; the old browser must not stop its turn
    try:
        for path, body in (
            ("/api/turn", {"text": "late"}),
            ("/api/command", {"line": "/rename late"}),
            ("/api/command", {"line": "/new"}),
            ("/api/stop", {}),
            ("/api/handoff", {}),
            ("/api/go", {"id": "N1"}),
            ("/api/pending", {"edges": []}),
            ("/api/reset", {}),
            ("/api/attachments", {"attachments": []}),
        ):
            req = urllib.request.Request(
                base + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-Kaipi-Token": token},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            assert error.value.code == 410
            assert "画布已关闭" in json.loads(error.value.read())["error"]
        assert not canvas.s.stopping.is_set()
        assert before == (canvas.s.log.path.read_bytes(), canvas.s.cursor_path.read_bytes())
    finally:
        canvas.s.idle.set()


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


def test_canvas_accepts_attachment_snapshots_and_rejects_invalid_data(srv, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import base64

    from tests.test_attachments import PNG

    canvas, call, _, _ = srv
    seen: list[Message] = []

    class Inspect(Echo):
        def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
            seen.extend(messages)
            return Reply(
                content=[{"type": "text", "text": "seen image"}],
                stop_reason="end_turn",
                usage=Usage(),
            )

    monkeypatch.setattr(core.Session, "_build", lambda self, model: Inspect())
    before = canvas.s.log.path.read_bytes()
    metadata = call(
        "/api/attachments",
        {"attachments": [{"name": "image.png", "data": base64.b64encode(PNG).decode()}]},
    )["attachments"][0]
    assert metadata["kind"] == "image" and metadata["name"] == "image.png"
    assert set(metadata) == {"name", "summary", "kind"}, "draft markers need no thumbnail payload"
    assert canvas.s.log.path.read_bytes() == before

    call(
        "/api/turn",
        {
            "text": "解释这张图",
            "attachments": [{"name": "image.png", "data": base64.b64encode(PNG).decode()}],
        },
    )
    _wait(canvas)
    assert seen[0]["content"][1]["type"] == "image"
    assert seen[0]["content"][1]["source"]["data"] == base64.b64encode(PNG).decode()
    leaf = canvas.s.leaf
    assert leaf is not None
    replay = call("/api/node/" + leaf)
    assert any(row["kind"] == "image" for row in replay)
    before = canvas.s.log.path.read_bytes()
    response = call("/api/turn", {"text": "bad", "attachments": [{"name": "bad", "data": "!!!"}]})
    assert "error" in response and not canvas.busy
    assert canvas.s.log.path.read_bytes() == before


@pytest.mark.parametrize("phase", ["parse", "response"])
def test_slow_attachment_request_does_not_block_stop(srv, monkeypatch, phase: str) -> None:  # type: ignore[no-untyped-def]
    canvas, call, _, _ = srv
    entered, release = threading.Event(), threading.Event()
    send = server.Handler._json

    def pause(*args: object) -> None:
        entered.set()
        assert release.wait(5)

    def slow_response(handler: server.Handler, obj: Any, status: int = 200) -> None:
        if handler.path == "/api/attachments":
            pause()
        send(handler, obj, status)

    if phase == "parse":
        monkeypatch.setattr(attachments, "validate", pause)
    else:
        monkeypatch.setattr(server.Handler, "_json", slow_response)
    canvas.s.claim()
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(call, "/api/attachments", {"attachments": []})
        try:
            assert entered.wait(2)
            assert pool.submit(call, "/api/stop", {}).result(timeout=2) == {"ok": True}
            assert canvas.s.stopping.is_set()
        finally:
            release.set()
            canvas.s.idle.set()
        assert request.result(timeout=2) == {"attachments": []}


@pytest.mark.parametrize("change", ["new", "go", "handoff"])
def test_turn_rechecks_session_after_parsing(srv, monkeypatch, change: str) -> None:  # type: ignore[no-untyped-def]
    canvas, call, _, _ = srv
    call("/api/turn", {"text": "first"})
    _wait(canvas)
    root = canvas.s.leaf
    call("/api/turn", {"text": "second"})
    _wait(canvas)
    log = canvas.s.log
    before = log.path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    parse = core.input_text

    def slow_parse(text: str, items: list[Any]) -> str:
        entered.set()
        assert release.wait(5)
        return parse(text, items)

    monkeypatch.setattr(core, "input_text", slow_parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(call, "/api/turn", {"text": "stale request"})
        try:
            assert entered.wait(2)
            path, body = {
                "new": ("/api/command", {"line": "/new"}),
                "go": ("/api/go", {"id": root}),
                "handoff": ("/api/handoff", {}),
            }[change]
            assert "error" not in pool.submit(call, path, body).result(timeout=2)
            after_change = log.path.read_bytes()
        finally:
            release.set()
        result = request.result(timeout=2)
    expected = "画布已关闭" if change == "handoff" else "会话位置已变化"
    assert expected in result["error"]
    assert log.path.read_bytes() == after_change
    assert len(log.state.nodes) == 2 and not canvas.busy
    if change != "go":
        assert after_change == before
    if change == "new":
        assert not canvas.s.log.state.nodes
