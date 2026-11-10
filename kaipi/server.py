"""Local HTTP interface + the canvas front-end. One process, one session, one surface at a
time: the CLI hands the session to the canvas with /canvas and takes it back with /cli."""

from __future__ import annotations

import json
import queue
import secrets
import threading
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from kaipi import attachments, context, core, i18n, views
from kaipi.i18n import tr
from kaipi.model import Node, ReferenceEdge, blocks, text_of

CANVAS_HTML = Path(__file__).with_name("canvas.html")


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
            elif t == "image":
                src = b["source"]
                out.append(
                    {"kind": "image", "text": f"data:{src['media_type']};base64,{src['data']}"}
                )
            elif t == "document":
                out.append({"kind": "attachment", "text": b.get("title", tr("PDF 附件"))})
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
ID_VERBS: dict[str, Callable[[core.Session, str], object]] = {
    "/api/go": lambda s, ref: s.go(ref),
    "/api/archive": lambda s, ref: s.set_status(ref, restore=False),
    "/api/restore": lambda s, ref: s.set_status(ref, restore=True),
    "/api/pin": lambda s, ref: s.pin(ref),
}


class ClosedError(ValueError):
    """This client has handed the session off and cannot issue further operations."""


class Canvas:
    """Browser adapter: core owns execution, state and persistence."""

    def __init__(self, s: core.Session, hub: Hub) -> None:
        self.s, self.hub = s, hub
        self.handoff = False
        self.on_handoff: Callable[[], None] = lambda: None

    @property
    def busy(self) -> bool:
        return self.s.busy

    @contextmanager
    def active(self) -> Iterator[None]:
        with self.s.lock:
            if self.handoff:
                raise ClosedError(tr("画布已关闭，请在终端继续此会话"))
            yield

    def turn(self, text: str, explore: bool = False) -> None:
        with self.active():
            self.s.start(text, self.event, explore=explore)

    def event(self, ev: core.Event) -> None:
        if ev.kind == "start":
            self.hub.publish(type="start", text=ev.text, exploration=ev.exploration)
        elif ev.kind == "completed":
            self.hub.publish(
                type="done", node=ev.node_id, dirty=self.s.dirty, exploration=ev.exploration
            )
        elif ev.kind == "aborted":
            n = self.s.log.state.nodes[ev.node_id]
            self.hub.publish(
                type="aborted",
                node=n.id,
                leaf=self.s.leaf,
                cost=f"{self.s.pricing.price(n.model).cost(n.usage):.4f}",
                retryable=ev.retryable,
                error=ev.text,
            )
        else:
            rendered = views.event(self.s, ev)
            if rendered:
                kind, text = rendered
                self.hub.publish(type=kind, text=views.plain(text) if kind == "ledger" else text)

    def request_handoff(self) -> None:
        with self.active():
            self.s.require_idle()
            self.s.save()
            self.handoff = True
        self.hub.publish(type="handoff", to="cli")
        self.on_handoff()

    def command(self, line: str) -> str:
        """A slash command typed into the canvas; returns what the CLI would have printed."""
        with self.active():
            argv = line.lstrip("/").split()
            if not argv:
                return ""
            if argv[0] in ("cli", "quit", "exit"):
                self.request_handoff()
                return tr("handing the session back to the terminal")
            if argv[0] == "canvas":
                return tr("already on the canvas")
            if argv[0] == "retry":
                if len(argv) != 1:
                    raise ValueError(tr("用法：/retry（不带参数）"))
                self.s.start_retry(self.event)
                return ""
            if argv[0] == "explore":  # reserve here; the worker executes outside the lock
                self.turn(" ".join(argv[1:]), explore=True)
                return tr("exploration started")
            self.s.require_idle()
            try:
                out = views.verb(self.s, argv)
            except Exception as e:  # noqa: BLE001 - surfaced to the user verbatim
                return tr("错误：{error}", error=e)
            if out is not None:
                return views.plain("\n".join(out))
            # Interactive commands still need the terminal.
            if argv[0] == "rewind" and len(argv) == 2:
                return views.rewind_preview(self.s, argv[1]) + tr(
                    "\nuse the node's right-click menu, or /rewind <id> both|code|conversation"
                )
            if argv[0] == "resume":
                return "\n".join(
                    line for _, line in views.session_rows(self.s.cwd, self.s.log.path.name)
                )
            if argv[0] in {n for n, _, _ in views.COMMANDS}:
                return tr("/{v0} needs the terminal: /cli, then run it there", v0=argv[0])
            return tr("unknown command /{v0}", v0=argv[0])


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
            self._json({"error": tr("not a local, token-carrying request")}, 403)
            return
        try:
            self._get()
        except (ValueError, KeyError) as e:
            self._json({"error": str(e)}, 400)

    def _get(self) -> None:
        c = self.server.canvas
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            body = i18n.canvas_page(CANVAS_HTML.read_text(encoding="utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/state":
            self._json(c.s.state())
        elif url.path.startswith("/api/node/"):
            nid = c.s.resolve(url.path.rsplit("/", 1)[1])
            self._json(transcript(c.s.log.state.nodes[nid]))
        elif url.path == "/api/preview":
            tools = [t for t in q.get("tools", [""])[0].split(",") if t]
            self._json(c.s.preview(q["src"][0], tools))
        elif url.path == "/api/rewind":
            nid = c.s.resolve(q["id"][0])
            n = c.s.log.state.nodes[nid]
            self._json({"paths": core.rewind_paths(c.s.log.state, nid), "tree": bool(n.tree)})
        elif url.path == "/api/events":
            self._events()
        else:
            self._json({"error": tr("not found")}, 404)

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
            self._json({"error": tr("not a local, token-carrying JSON request")}, 403)
            return
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= 56 * 1024 * 1024:
            self.close_connection = True
            self._json({"error": tr("请求过大；原始附件总大小最多 40 MiB")}, 413)
            return
        path = urlparse(self.path).path
        try:
            body: dict[str, Any] = json.loads(self.rfile.read(n) or b"{}")
            with c.active():
                position = c.s.log, c.s.leaf, list(c.s.pending)
                if path not in ("/api/attachments", "/api/handoff", "/api/stop"):
                    c.s.require_idle()
            result: dict[str, Any] = {"ok": True}
            status = 200
            # Decoding files can be slow. Keep both parsing and socket writes outside the gate.
            if path == "/api/attachments":
                items = [
                    attachments.Attachment.model_validate(item) for item in body["attachments"]
                ]
                attachments.validate(items)
                result = {"attachments": [item.describe() for item in items]}
            elif path == "/api/turn":
                text = core.input_text(str(body["text"]), body.get("attachments", []))
                if not text.strip():
                    raise ValueError(tr("请输入问题或添加附件"))
            with c.active():
                if path in ("/api/attachments", "/api/turn") and position != (
                    c.s.log,
                    c.s.leaf,
                    c.s.pending,
                ):
                    raise core.BusyError(tr("会话位置已变化，请重新发送"))
                if path == "/api/attachments":
                    pass  # draft inspection never mutates a session
                elif path == "/api/handoff":
                    c.request_handoff()
                elif path == "/api/stop":
                    c.s.stop()
                else:
                    c.s.require_idle()
                    if path == "/api/turn":
                        c.turn(text, bool(body.get("explore")))
                    elif path == "/api/retry":
                        if body:
                            raise ValueError(tr("用法：/retry（不带参数）"))
                        c.s.start_retry(c.event)
                    elif path == "/api/command":
                        result = {"output": c.command(str(body["line"]))}
                    elif path in ID_VERBS:
                        ID_VERBS[path](c.s, str(body["id"]))
                    elif path == "/api/rewind":
                        c.s.rewind(str(body["id"]), str(body.get("mode", "both")))
                    elif path == "/api/pending":
                        c.s.stage([ReferenceEdge(**e) for e in body["edges"]])
                    elif path == "/api/reset":
                        c.s.reset()
                    else:
                        result, status = {"error": tr("not found")}, 404
            self._json(result, status)
        except ClosedError as e:
            self._json({"error": str(e)}, 410)
        except core.BusyError as e:
            self._json({"error": str(e)}, 409)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


class KaipiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, canvas: Canvas, port: int, token: str = "") -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.canvas = canvas
        self.token = token or secrets.token_urlsafe(32)


def serve(s: core.Session, port: int = 0, *, open_browser: bool = True) -> str:
    """Run the canvas until it hands the session back (or Ctrl-C). Returns the URL used."""
    canvas = Canvas(s, Hub())
    httpd = KaipiServer(canvas, port)
    canvas.on_handoff = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={httpd.token}"
    core.set_lock(s.cwd, "canvas", url)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        s.stop()
        s.idle.wait()
        canvas.request_handoff()
    finally:
        httpd.server_close()
    return url
