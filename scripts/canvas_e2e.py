#!/usr/bin/env python3
"""Drive the canvas in a real browser against the mock endpoint.

Everything below the browser is production code: the canvas page, the HTTP server, the CLI
verbs, the provider and its HTTP client. Only the model is mocked. This covers the gestures
that have no other test - drag-to-graft, the tool-output picker, drag-to-archive, the rewind
dialog, the handoff - plus dark mode, narrow viewports and console errors.

    uv run python scripts/canvas_e2e.py            # exits non-zero if any check fails
    uv run python scripts/canvas_e2e.py --shots /tmp   # also write screenshots

Needs Playwright and a Chromium; set PLAYWRIGHT_CHROMIUM to a binary if it is not on PATH.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mockapi  # noqa: E402
import smoke  # noqa: E402

from kaipi import cli, server  # noqa: E402

CHECKS = smoke.Checks()  # same reporter (and same PASS/FAIL/SKIP wording) as the smoke test


def check(ok: object, name: str, detail: object = "") -> None:
    CHECKS.add(bool(ok), name, str(detail))


def _box(pg: Any, selector: str) -> dict[str, float]:
    box = pg.locator(selector).bounding_box()
    assert box is not None, f"{selector} is not on screen"
    return dict(box)


def chromium() -> str | None:
    env = os.environ.get("PLAYWRIGHT_CHROMIUM")
    if env:
        return env
    for c in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"):
        return str(c)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shots", type=Path, help="directory to write screenshots into")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed; skipping (pip install playwright)")
        return 0

    api, url = mockapi.start()
    os.environ["ANTHROPIC_BASE_URL"] = url
    os.environ["ANTHROPIC_API_KEY"] = "mock"
    os.environ["KAIPI_MODEL"] = "claude-opus-5"
    root = Path(tempfile.mkdtemp(prefix="kaipi-canvas-"))
    smoke.make_repo(root)
    os.chdir(root)

    s = cli.Session(root, new=True)
    canvas = server.Canvas(s, server.Hub())
    httpd = server.KaipiServer(canvas, args.port)
    canvas.on_handoff = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={httpd.token}"

    def shot(name: str, pg: Any) -> None:
        if args.shots:
            pg.screenshot(path=str(args.shots / f"canvas-{name}.png"))

    api_token = httpd.token

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium())
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(page_url)
        pg.wait_for_selector("#empty")

        def say(text: str, explore: bool = False) -> None:
            if explore:
                pg.check("#explore")
            pg.fill("#input", text)
            pg.press("#input", "Enter")
            pg.wait_for_function(
                "!document.querySelector('#send').disabled && !document.querySelector('.live')",
                timeout=30000,
            )
            time.sleep(0.3)

        say("Inspect app.py and tell me what the bug is. Change nothing yet.")
        check(pg.evaluate("S.nodes.length") == 1, "first turn created one node")
        check("rate_limit" in pg.inner_text("#transcript"), "the answer reaches the transcript")

        say("Now fix the bug in app.py and run the tests.")
        ids = [n["id"] for n in pg.evaluate("S.nodes")]
        check(len(ids) == 2, "second turn extends the trunk")

        pg.click(f'.node[data-id="{ids[0]}"]')
        time.sleep(0.4)
        check(pg.inner_text("#curkind") == "探索", "clicking a non-trunk node arms an exploration")
        say("Only look: is there any other hard-coded number in the repo?")
        pg.click(f'.node[data-id="{ids[0]}"]')
        time.sleep(0.4)
        say("Only look: do the tests cover the boundary at LIMIT?")
        ids = [n["id"] for n in pg.evaluate("S.nodes")]
        check(len(ids) == 4, "two sibling explorations exist")
        check(pg.evaluate("S.trunk") == ids[1], "the trunk did not move to an exploration")

        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
        ).stdout
        check(
            "??" not in dirty and ".kaipi" not in dirty,
            "explorations created no files, and .kaipi ignores itself",
            dirty.strip()[:60] or "clean",
        )

        # drag an exploration onto the composer: this is the graft gesture
        pg.click(f'.node[data-id="{ids[1]}"]')
        time.sleep(0.3)
        box = _box(pg, f'.node[data-id="{ids[3]}"] rect.card')
        drop = _box(pg, "#drop")
        pg.mouse.move(box["x"] + 40, box["y"] + 20)
        pg.mouse.down()
        pg.mouse.move(box["x"] + 90, box["y"] + 70, steps=5)
        pg.mouse.move(drop["x"] + drop["width"] / 2, drop["y"] + drop["height"] / 2, steps=10)
        check(pg.locator("#drop.over").count() == 1, "the drop zone highlights while dragging")
        shot("dragging", pg)
        pg.mouse.up()
        time.sleep(1.2)
        check(pg.locator(".chip").count() == 1, "dropping created a graft chip")
        check("+" in pg.inner_text("#delta"), "the chip previews the trunk-burden delta")
        opts = pg.eval_on_selector(".chip select", "e => [...e.options].map(o => o.textContent)")
        check(all("+" in o for o in opts), "all three depths are priced", opts)
        shot("graft", pg)

        pg.click(".chip .tools")
        time.sleep(0.4)
        check(
            pg.locator(".pop input[type=checkbox]").count() >= 1,
            "the tool-output picker lists commands",
        )
        pg.check(".pop input[type=checkbox] >> nth=0")
        time.sleep(0.8)
        check(
            pg.locator(".chip .tools").inner_text().startswith("+1"),
            "ticking a tool output updates the chip",
        )
        pg.keyboard.press("Escape")

        say("Using the grafted finding, add the missing boundary test and run the suite again.")
        leaf = pg.evaluate("S.leaf")
        node = next(x for x in pg.evaluate("S.nodes") if x["id"] == leaf)
        check(node["grafts"] and node["grafts"][0] == ids[3], "the graft is recorded on the node")
        check(pg.evaluate("S.edges.length") == 1, "a reference edge is drawn")

        # drag the other exploration onto the tray: this is the archive gesture
        box = _box(pg, f'.node[data-id="{ids[2]}"] rect.card')
        tray = _box(pg, "#tray")
        pg.mouse.move(box["x"] + 40, box["y"] + 20)
        pg.mouse.down()
        pg.mouse.move(box["x"] + 60, box["y"] + 60, steps=5)
        pg.mouse.move(tray["x"] + tray["width"] / 2, tray["y"] + tray["height"] / 2, steps=10)
        pg.mouse.up()
        time.sleep(1.0)
        status = {x["id"]: x["status"] for x in pg.evaluate("S.nodes")}
        check(status[ids[2]] == "archived", "dragging to the tray archived the subtree")
        check("已归档" in pg.inner_text("#toast"), "the archive toast says how to undo it")

        pg.click(f'.node[data-id="{ids[1]}"]', button="right")
        time.sleep(0.3)
        pg.click('#menu button[data-a="rewind"]')
        time.sleep(0.6)
        files = pg.inner_text("#rewind .files")
        check(".py" in files, "the rewind dialog lists the files it would restore", files[:50])
        shot("rewind", pg)
        pg.click('#rewind button[data-m="conversation"]')
        time.sleep(0.8)
        check(pg.evaluate("S.leaf") == ids[1], "conversation-only rewind moved just the cursor")

        pg.fill("#input", "/tree")
        pg.press("#input", "Enter")
        time.sleep(0.8)
        check("trunk burden" in pg.inner_text("#transcript"), "slash output survives the refresh")

        pg.emulate_media(color_scheme="dark")
        time.sleep(0.4)
        shot("dark", pg)
        bg = pg.eval_on_selector("body", "e => getComputedStyle(e).backgroundColor")
        check(bg == "rgb(18, 22, 20)", "dark mode repaints the page", bg)
        pg.emulate_media(color_scheme="light")

        pg.set_viewport_size({"width": 900, "height": 800})
        time.sleep(0.4)
        check(
            pg.eval_on_selector("body", "e => e.scrollWidth <= e.clientWidth"),
            "no horizontal overflow at 900px",
        )
        pg.set_viewport_size({"width": 1500, "height": 950})

        # the stop button: it has to appear while a turn runs, discard that turn, and say so
        check(pg.locator("#stop").is_hidden(), "the stop button is hidden when idle")
        bar = pg.evaluate("S.status")
        check(
            bar["model"] and bar["provider"] and "saved" in bar,
            "the status bar carries provider, model and the savings breakdown",
            f"{bar['provider']}/{bar['model']} window={bar['window']}",
        )
        check(
            "/" in pg.inner_text("#burden") and "$" in pg.inner_text("#cost"),
            "context window and cumulative spend are on screen",
            pg.inner_text("#burden") + "  " + pg.inner_text("#cost"),
        )
        cost = pg.locator(".node .cost").first.text_content() or ""
        check("$" in cost, "each node shows what that turn cost", cost)

        # make the next turn slow enough to interrupt: its test run now blocks for 20s
        (root / "test_app.py").write_text(
            (root / "test_app.py").read_text() + "\n\nimport time\n\ntime.sleep(20)\n"
        )
        pg.fill("#input", "fix the bug in app.py and run the tests")
        pg.press("#input", "Enter")
        pg.wait_for_selector("#stop:not([hidden])", timeout=15000)
        check(True, "the stop button appears while a turn is running")
        pg.wait_for_function(
            "[...document.querySelectorAll('.ln.cmd')]"
            ".some(e => e.textContent.includes('test_app'))",
            timeout=20000,
        )
        pg.click("#stop")
        pg.wait_for_function("!document.querySelector('#send').disabled", timeout=20000)
        time.sleep(0.6)
        nodes = pg.evaluate("S.nodes")
        dead = [n for n in nodes if n["status"] == "aborted"]
        check(len(dead) == 1, "the stopped turn is kept as one aborted node", str(len(nodes)))
        check(pg.locator(".node.aborted").count() == 1, "and is drawn as discarded on the canvas")
        check(
            pg.locator(".node.aborted .strike").count() == 1
            and "已废弃" in (pg.locator(".node.aborted").first.text_content() or ""),
            "struck through and labelled, so it cannot be mistaken for live context",
        )
        check(
            pg.evaluate("S.leaf") == dead[0]["parent_id"],
            "the cursor moved back to the parent: the next input is a sibling",
        )
        check(
            "已停止" in pg.inner_text("#transcript"),
            "the transcript says the turn was discarded",
        )
        saved = pg.evaluate("S.status.saved")
        check(
            saved["interrupt"]["tokens"] > 0,
            "the discarded turn is counted as tokens kept out of the context",
            str(saved["interrupt"]),
        )
        shot("aborted", pg)

        pg.click("#handoff")
        time.sleep(1.0)
        check(
            pg.locator("#overlay.show").count() == 1, "handoff shows the back-to-terminal overlay"
        )
        check(canvas.handoff, "the server was told to hand off")
        check(not errors, "no console errors anywhere", errors[:2])
        # the page has to be sending the token, or none of the above would have worked
        sent = pg.evaluate("TOKEN")
        check(sent and sent == api_token, "the page carries the per-run token")
        browser.close()

    failed = CHECKS.failed
    print(f"{failed} FAILED" if failed else "ALL CANVAS CHECKS PASSED")
    print(f"{len(api.api.requests)} requests reached the mock endpoint")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
