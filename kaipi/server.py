"""Local HTTP interface + the canvas front-end. One process, one session, one surface at a
time: the CLI hands the session to the canvas with /canvas and takes it back with /cli."""

from __future__ import annotations

import contextlib
import io
import json
import queue
import re
import secrets
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer

from kaipi import cli, context, graph, guard, ledger
from kaipi.model import Node, ReferenceEdge, blocks, text_of

CANVAS_HTML = Path(__file__).with_name("canvas.html")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Hub:
    """Fan-out of server-sent events to every open canvas tab."""

    def __init__(self) -> None:
        self.clients: list[queue.Queue[str]] = []
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, **event: Any) -> None:
        data = json.dumps(event, ensure_ascii=False)
        with self.lock:
            for q in self.clients:
                q.put(data)


def transcript(n: Node) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in n.payload:
        for b in blocks(m["content"]):
            t = b.get("type")
            if t == "text" and m["role"] == "user":
                out.append({"kind": context.classify(b["text"]), "text": b["text"]})
            elif t == "text":
                out.append({"kind": "assistant", "text": b["text"]})
            elif t == "tool_use":
                out.append(
                    {
                        "kind": "cmd",
                        "text": str(b.get("input", {}).get("command", "")),
                        "id": b["id"],
                    }
                )
            elif t == "tool_result":
                out.append({"kind": "out", "text": text_of(b.get("content", ""))})
    return out


# The verbs that take one node id and nothing else.
ID_VERBS: dict[str, Callable[[cli.Session, str], None]] = {
    "/api/go": cli.cmd_go,
    "/api/archive": cli.cmd_archive,
    "/api/restore": cli.cmd_restore,
    "/api/pin": cli.cmd_trunk,
}


class Canvas:
    """The session as seen by the browser. All mutations go through one lock."""

    def __init__(self, s: cli.Session, hub: Hub) -> None:
        self.s = s
        self.hub = hub
        self.repo = guard.is_repo(s.cwd)  # constant for the session
        self.lock = threading.RLock()
        self.busy = False
        self.dirty = False
        self.handoff = False
        self.on_handoff: Callable[[], None] = lambda: None  # set by serve(): stops the server

    def state(self) -> dict[str, Any]:
        s, st = self.s, self.s.log.state
        t = graph.trunk(st)
        nodes = []
        for n in st.nodes.values():
            nodes.append(
                {
                    "id": n.id,
                    "parent_id": n.parent_id,
                    "status": n.status,
                    "seq": n.seq,
                    "input": context.user_input(n),
                    "answer": context.final_answer(n),
                    "own": ledger.own_tokens(st, n.id),
                    "ctx": n.context_tokens,
                    "tools": [{"id": i, "cmd": c} for i, c in context.tool_calls(n)],
                    "grafts": [g.src_id for g in n.grafts],
                    "cost": s.pricing.price(n.model).cost(n.usage),
                    "dropped": n.dropped_thinking,
                }
            )
        return {
            "session": s.log.path.stem,
            "model": st.model,
            "nodes": nodes,
            "edges": [e.model_dump() for e in st.edges],
            "trunk": t,
            "pin": st.trunk_pin,
            "leaf": s.leaf,
            "pending": [e.model_dump() for e in s.pending],
            "burden": ledger.trunk_burden(st),
            "cost": ledger.total_cost(st, s.pricing),
            "busy": self.busy,
            "dirty": self.dirty,
            "repo": self.repo,
        }

    def preview(self, src: str, tools: list[str]) -> dict[str, Any]:
        edge = ReferenceEdge(src_id=src, dst_id="", include_tool_outputs=tools)
        st = self.s.log.state
        sizes = ledger.graft_preview(st, self.s.leaf, edge, self.s.provider.count_tokens)
        return {
            "sizes": sizes,
            "burden": ledger.trunk_burden(st),
            "on_trunk": self.s.leaf == graph.trunk(st),
        }

    def command(self, line: str) -> str:
        """A slash command typed into the canvas; returns what the CLI would have printed."""
        argv = line.lstrip("/").split()
        if not argv:
            return ""
        if argv[0] in ("cli", "quit", "exit"):
            self.request_handoff()
            return "handing the session back to the terminal"
        if argv[0] == "canvas":
            return "already on the canvas"
        if argv[0] == "explore":  # a model turn: must stream, must not hold the lock
            self.turn(" ".join(argv[1:]), explore=True)
            return "exploration started"
        if argv[0] == "rewind" and len(argv) == 2:  # the CLI would prompt; the canvas can't
            nid = self.s.resolve(argv[1])
            paths = cli.rewind_paths(self.s.log.state, nid)
            return (
                f"a code rewind to {cli.short(nid)} would restore: {', '.join(paths) or '(none)'}"
                "\nuse the node's right-click menu, or /rewind <id> both|code|conversation"
            )
        buf = io.StringIO()
        with self.lock, contextlib.redirect_stdout(buf):
            try:
                cli.slash(self.s, argv)
            except Exception as e:  # noqa: BLE001 - surfaced to the user verbatim
                print(f"error: {e}")
        self.s.save()
        return _ANSI.sub("", buf.getvalue()).rstrip()

    def turn(self, text: str, explore: bool = False) -> None:
        with self.lock:
            if self.busy:
                raise RuntimeError("a turn is already running")
            self.busy = True
        threading.Thread(target=self._run, args=(text, explore), daemon=True).start()

    def _run(self, text: str, explore: bool) -> None:
        st = self.s.log.state
        exploration = graph.is_exploration(st, self.s.leaf, force=explore)
        self.hub.publish(type="start", exploration=exploration)
        try:
            nid, dirty = cli.run_input(
                self.s, text, hook=lambda k, t: self.hub.publish(type=k, text=t), explore=explore
            )
            self.dirty = dirty
            self.s.save()
            self.hub.publish(type="done", node=nid, dirty=dirty, exploration=exploration)
        except Exception as e:  # noqa: BLE001
            self.hub.publish(type="error", text=str(e))
        finally:
            self.busy = False

    def request_handoff(self) -> None:
        self.handoff = True
        self.hub.publish(type="handoff", to="cli")
        self.on_handoff()


class Handler(BaseHTTPRequestHandler):
    server: KaipiServer

    def log_message(self, *_: Any) -> None:  # quiet
        pass

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        """Three checks, because this endpoint can run bash.

        The Host header must name loopback and this exact port: comparing Origin against the
        client's own Host would accept a DNS-rebound name (`evil.com` re-resolved to
        127.0.0.1 sends Origin and Host both saying `evil.com`, and they match).
        Origin, when present, must be the address we actually serve.
        And every request must carry the per-run token that only the URL we opened contains,
        which is what stops another local user - or a rebound page that guessed the port -
        from driving the agent."""
        host = self.headers.get("Host", "")
        name, _, port = host.rpartition(":")
        expected = str(self.server.server_address[1])
        if name.strip("[]") not in ("127.0.0.1", "localhost", "::1") or port != expected:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (
            f"http://127.0.0.1:{expected}",
            f"http://localhost:{expected}",
        ):
            return False
        sent = (
            self.headers.get("X-Kaipi-Token")
            or parse_qs(urlparse(self.path).query).get("t", [""])[0]
        )
        return secrets.compare_digest(sent, self.server.token)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/favicon.ico":  # browsers ask before they have a token
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._authorised():
            self._json({"error": "not a local, token-carrying request"}, 403)
            return
        try:
            self._get()
        except (ValueError, KeyError, typer.BadParameter) as e:
            self._json({"error": str(e)}, 400)

    def _get(self) -> None:
        c = self.server.canvas
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            body = CANVAS_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/state":
            with c.lock:
                self._json(c.state())
        elif url.path.startswith("/api/node/"):
            nid = c.s.resolve(url.path.rsplit("/", 1)[1])
            self._json(transcript(c.s.log.state.nodes[nid]))
        elif url.path == "/api/preview":
            tools = [t for t in q.get("tools", [""])[0].split(",") if t]
            self._json(c.preview(c.s.resolve(q["src"][0]), tools))
        elif url.path == "/api/rewind":
            nid = c.s.resolve(q["id"][0])
            n = c.s.log.state.nodes[nid]
            self._json({"paths": cli.rewind_paths(c.s.log.state, nid), "tree": bool(n.tree)})
        elif url.path == "/api/events":
            self._events()
        else:
            self._json({"error": "not found"}, 404)

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = self.server.canvas.hub.subscribe()
        try:
            while not self.server.canvas.handoff:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.canvas.hub.unsubscribe(q)

    def do_POST(self) -> None:  # noqa: N802
        c = self.server.canvas
        ctype = self.headers.get("Content-Type", "")
        if not self._authorised() or not ctype.startswith("application/json"):
            # The JSON content type additionally forces a CORS preflight this server never
            # answers, so a plain cross-origin form post cannot reach any of this.
            self._json({"error": "not a local, token-carrying JSON request"}, 403)
            return
        n = int(self.headers.get("Content-Length") or 0)
        body: dict[str, Any] = json.loads(self.rfile.read(n) or b"{}")
        path = urlparse(self.path).path
        try:
            if path == "/api/handoff":
                c.request_handoff()
                self._json({"ok": True})
                return
            if c.busy and path != "/api/pending":
                self._json({"error": "a turn is running"}, 409)
                return
            with c.lock:
                if path == "/api/turn":
                    c.turn(str(body["text"]), bool(body.get("explore")))
                elif path == "/api/command":
                    self._json({"output": c.command(str(body["line"]))})
                    return
                elif path in ID_VERBS:
                    ID_VERBS[path](c.s, str(body["id"]))
                elif path == "/api/rewind":
                    mode = str(body.get("mode", "both"))
                    cli.cmd_rewind(c.s, str(body["id"]), mode)
                    c.dirty = False
                elif path == "/api/pending":
                    c.s.pending = [ReferenceEdge(**e) for e in body["edges"]]
                elif path == "/api/reset":
                    guard.reset(c.s.cwd)
                    c.dirty = False
                else:
                    self._json({"error": "not found"}, 404)
                    return
                c.s.save()
            self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


class KaipiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, canvas: Canvas, port: int, token: str = "") -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.canvas = canvas
        self.token = token or secrets.token_urlsafe(32)


def serve(s: cli.Session, port: int = 0, *, open_browser: bool = True) -> str:
    """Run the canvas until it hands the session back (or Ctrl-C). Returns the URL used."""
    canvas = Canvas(s, Hub())
    httpd = KaipiServer(canvas, port)
    canvas.on_handoff = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={httpd.token}"
    cli.set_lock(s.cwd, "canvas", url)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        canvas.request_handoff()
    finally:
        httpd.server_close()
    return url
