#!/usr/bin/env python3
"""Real-API smoke test (CONCEPTS/startup-prompt Phase 2 acceptance).

Spends real money. It runs the scenario the startup prompt asks for - two explorations from
one fork, archive one, graft the other onto the trunk - in a throwaway git repo, and checks
the four things unit tests with a fake provider cannot:

  1. the ledger's four token counts equal the sum of what the API itself reported;
  2. prompt caching actually happens (a second trunk turn reads the first one back);
  3. sibling explorations share the cached prefix up to their fork point;
  4. reasoning state (Anthropic thinking blocks / OpenAI reasoning items / Gemini thought
     signatures) survives a multi-step tool loop and being replayed on the next turn.

Usage:
    export ANTHROPIC_API_KEY=...            # or the key the chosen model needs
    uv run python scripts/smoke.py                          # default model from pricing.toml
    uv run python scripts/smoke.py --model gpt-5.6-terra
    uv run python scripts/smoke.py --model gemini-anthropic/kimi-k3[1m] --keep

Exit code 0 = every check passed. Checks that cannot apply to the chosen provider are
reported as SKIP and do not fail the run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kaipi import cli, context, graph, guard, ledger  # noqa: E402
from kaipi.model import Message, NodeArchived, ReferenceEdge, Usage  # noqa: E402
from kaipi.providers import Provider, Reply  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

# A repo small enough to be cheap and real enough that the model must run commands.
FILES = {
    "app.py": '''"""A tiny service with one planted bug."""


def rate_limit(requests: int) -> bool:
    # BUG: the threshold is hard-coded instead of reading LIMIT
    return requests > 100


LIMIT = 250
''',
    "test_app.py": """from app import LIMIT, rate_limit


def test_under_limit() -> None:
    assert rate_limit(LIMIT - 1) is False


def test_over_limit() -> None:
    assert rate_limit(LIMIT + 1) is True
""",
    "AGENTS.md": "Run tests with `python -m pytest -q`. Keep changes minimal.\n",
}


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, ok: bool | None, name: str, detail: str = "") -> None:
        state = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.rows.append((state, name, detail))
        colour = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}[state]
        print(f"  {colour}{state}{RESET} {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for state, _, _ in self.rows if state == "FAIL")


class Tally:
    """Wraps a provider and independently sums what the API reported, so the ledger can be
    checked against it rather than against itself."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.model = inner.model
        self.usage = Usage()
        self.replies: list[Reply] = []

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        r = self.inner.complete(system, messages, cache_points)
        self.usage = self.usage + r.usage
        self.replies.append(r)
        u = r.usage
        print(
            f"    {DIM}api: uncached {u.input_uncached} write {u.cache_write} "
            f"read {u.cache_read} out {u.output} stop={r.stop_reason}{RESET}"
        )
        return r

    def count_tokens(self, text: str) -> int:
        return self.inner.count_tokens(text)

    def reasoning_blocks(self) -> int:
        """Blocks that carry provider reasoning state and must be replayed verbatim."""
        n = 0
        for r in self.replies:
            for b in r.content:
                if b.get("type") in ("thinking", "redacted_thinking", "reasoning"):
                    n += 1
                elif b.get("signature"):  # Gemini thoughtSignature rides on the block
                    n += 1
        return n


class FakeProvider:
    """Canned replies for --fake. Mimics one bash call then an answer, and fakes cache reads
    so the script's own assertions are exercised end to end."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0
        self.step = 0
        self.turn = 0
        self.prev = 0  # payload size of the previous request: what a real cache would serve

    def complete(self, system: str, messages: list[Message], cache_points: list[int]) -> Reply:
        self.step += 1
        blob = json.dumps(messages, ensure_ascii=False)
        readonly = context.GUARD_TAG in blob.rsplit("kaipi:graft", 1)[-1][-4000:]
        size = len(blob) // 4
        cached = min(self.prev, size)
        self.prev = size
        usage = Usage(input_uncached=size - cached, cache_write=0, cache_read=cached, output=40)
        if self.step % 2 == 1:
            self.calls += 1
            return Reply(
                content=[
                    {"type": "thinking", "thinking": "", "signature": f"sig{self.step}"},
                    {
                        "type": "tool_use",
                        "id": f"toolu_{self.calls:02d}",
                        "name": "bash",
                        # a well-behaved model reads first, writes only when asked to fix,
                        # and never writes on a read-only exploration
                        "input": {
                            "command": "sed -n 1,8p app.py"
                            if readonly or self.step <= 2
                            else "sed -i 's/requests > 100/requests > LIMIT/' app.py"
                        },
                    },
                ],
                stop_reason="tool_use",
                usage=usage,
            )
        return Reply(
            content=[{"type": "text", "text": "(fake answer)"}], stop_reason="end_turn", usage=usage
        )

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


def make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in FILES.items():
        (root / name).write_text(body)
    git(root, "init", "-q")
    git(root, "-c", "user.email=smoke@kaipi", "-c", "user.name=smoke", "add", "-A")
    git(
        root,
        "-c",
        "user.email=smoke@kaipi",
        "-c",
        "user.name=smoke",
        "commit",
        "-q",
        "-m",
        "initial",
    )


def turn(
    session: cli.Session, provider: Tally, text: str, *, explore: bool = False
) -> tuple[str, bool]:
    label = "exploration" if explore or session.leaf != graph.trunk(session.log.state) else "trunk"
    print(f"\n{BOLD}> [{label}] {text}{RESET}")

    def hook(kind: str, t: str) -> None:
        if kind == "cmd":
            print(f"    {DIM}$ {t}{RESET}")
        elif kind == "out":
            head = t.strip().splitlines()[:2]
            print(f"    {DIM}| {' / '.join(head)[:120]}{RESET}")
        elif kind == "text":
            print(f"    {t.strip()[:200]}")
        elif kind in ("stop", "guard"):
            print(f"    {RED}{kind}: {t}{RESET}")

    nid, dirty = cli.run_input(session, text, hook, explore=explore)
    session.save()
    n = session.log.state.nodes[nid]
    print(
        f"    {DIM}node {cli.short(nid)}  usage {n.usage.model_dump()}  "
        f"ctx {n.context_tokens}  burden {ledger.trunk_burden(session.log.state)}{RESET}"
    )
    return nid, dirty


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--model", default=os.environ.get("KAIPI_MODEL"), help="model id or <provider>/<model>"
    )
    ap.add_argument("--dir", type=Path, help="use this directory instead of a temp one")
    ap.add_argument("--keep", action="store_true", help="keep the scratch repo for inspection")
    ap.add_argument(
        "--fake",
        action="store_true",
        help="exercise this script's own plumbing with a canned provider (NOT a smoke test: "
        "it proves nothing about any API, only that the script and the checks run)",
    )
    args = ap.parse_args()

    root = args.dir or Path(tempfile.mkdtemp(prefix="kaipi-smoke-"))
    made_temp = args.dir is None
    make_repo(root)
    os.chdir(root)
    if args.model:
        os.environ["KAIPI_MODEL"] = args.model

    session = cli.Session(root, new=True)
    model = session.log.state.model
    price = session.pricing.models.get(model)
    provider_name = price.provider if price else str(args.model or model).partition("/")[0]
    print(f"{BOLD}kaipi smoke{RESET}  model {model}  provider {provider_name}  repo {root}")
    if price is None:
        print(f"{YELLOW}note: {model} is not in pricing.toml, so cost will read 0{RESET}")

    tally = Tally(FakeProvider(model) if args.fake else session._build(model))
    session._provider = tally
    session._cheap = tally
    c = Checks()

    # --- trunk: two turns, the second must read the first back out of the cache -----------
    n1, _ = turn(
        session,
        tally,
        "Read app.py and test_app.py and tell me what the bug is. Do not change anything yet.",
    )
    u1 = session.log.state.nodes[n1].usage
    c.add(u1.output > 0 and u1.context > 0, "turn 1 reports usage", f"context {u1.context}")
    c.add(
        bool(context.tool_calls(session.log.state.nodes[n1])),
        "the model actually ran bash",
        f"{len(context.tool_calls(session.log.state.nodes[n1]))} call(s)",
    )

    n2, _ = turn(session, tally, "Now fix the bug in app.py and run the tests.")
    u2 = session.log.state.nodes[n2].usage
    supports_cache = provider_name != "ollama"
    if supports_cache:
        c.add(
            u2.cache_read >= u1.context * 0.5,
            "turn 2 reads the cached trunk prefix",
            f"cache_read {u2.cache_read} vs turn-1 context {u1.context}",
        )
    else:
        c.add(None, "turn 2 reads the cached trunk prefix", "provider does not report caching")

    c.add(
        session.log.state.nodes[n2].paths != [],
        "the trunk turn changed files on disk",
        ", ".join(session.log.state.nodes[n2].paths) or "(none)",
    )
    c.add(
        session.log.state.nodes[n2].tree is not None,
        "the node recorded a git snapshot for rewind",
        str(session.log.state.nodes[n2].tree or "")[:12],
    )

    # --- two explorations from the same fork point ----------------------------------------
    cli.cmd_go(session, n1)
    session.save()
    e1, _ = turn(session, tally, "Only look: is there any other hard-coded number in the repo?")
    fork_ctx = session.log.state.nodes[n1].context_tokens
    cli.cmd_go(session, n1)
    session.save()
    e2, _ = turn(
        session, tally, "Only look: do the tests cover the boundary value exactly at LIMIT?"
    )
    ue2 = session.log.state.nodes[e2].usage
    if supports_cache:
        c.add(
            ue2.cache_read >= fork_ctx * 0.5,
            "the second exploration shares the fork-point cache",
            f"cache_read {ue2.cache_read} vs fork context {fork_ctx}",
        )
    else:
        c.add(None, "the second exploration shares the fork-point cache", "no cache reporting")
    c.add(
        graph.fork_point(session.log.state, e2) == n1,
        "the fork point is where both explorations branch",
        cli.short(graph.fork_point(session.log.state, e2) or "-"),
    )
    c.add(
        guard.snapshot(root) == session.log.state.nodes[n2].tree,
        "explorations left the working tree untouched",
    )

    # --- reasoning state replay ------------------------------------------------------------
    stored = sum(
        1
        for n in session.log.state.nodes.values()
        for m in n.payload
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") in ("thinking", "redacted_thinking", "reasoning") or b.get("signature")
    )
    seen = tally.reasoning_blocks()
    if seen:
        c.add(
            stored >= seen,
            "reasoning state is frozen into payloads and replayed",
            f"{stored} stored / {seen} returned by the API",
        )
    else:
        c.add(None, "reasoning state is frozen into payloads and replayed", "model returned none")
    dropped = sum(n.dropped_thinking for n in session.log.state.nodes.values())
    c.add(
        dropped == 0,
        "the API dropped no reasoning blocks (history stayed append-only)",
        f"{dropped} dropped",
    )

    # --- archive one exploration, graft the other onto the trunk ---------------------------
    session.log.append(NodeArchived(ids=graph.subtree(session.log.state, e1)))
    c.add(
        session.log.state.nodes[e1].status == "archived",
        "archiving an exploration is atomic",
    )
    c.add(ledger.cache_color(session.log.state)[0] == "green", "the session stays cache-green")

    cli.cmd_go(session, n2)
    session.save()
    edge = ReferenceEdge(src_id=e2, dst_id="", depth="leaf+summary")
    preview = ledger.graft_preview(session.log.state, session.leaf, edge, tally.count_tokens)
    print(f"\n{DIM}graft preview: {preview}{RESET}")
    c.add(
        preview["leaf"] <= preview["branch"] and all(v > 0 for v in preview.values()),
        "graft preview sizes all three depths",
        str(preview),
    )
    burden_before = ledger.trunk_burden(session.log.state)
    session.pending = [edge]
    n3, _ = turn(
        session,
        tally,
        "Using the grafted finding, add the missing boundary test and run the suite.",
    )
    merged = session.log.state.nodes[n3]
    c.add(
        bool(merged.grafts) and merged.grafts[0].src_id == e2,
        "the graft block is snapshotted into the node",
    )
    c.add(
        context.GRAFT_TAG in json.dumps(merged.payload, ensure_ascii=False),
        "the graft block sits at the head of the new node's payload",
    )
    c.add(
        ledger.trunk_burden(session.log.state) > burden_before,
        "trunk burden grew by the graft plus the turn",
        f"{burden_before} -> {ledger.trunk_burden(session.log.state)}",
    )

    # --- the ledger must equal what the API reported ---------------------------------------
    report = ledger.report(session.log.state, session.pricing)
    print(f"\n{BOLD}ledger{RESET}  {report.usage.model_dump()}  cost ${report.total_cost:.4f}")
    print(f"{BOLD}api   {RESET}  {tally.usage.model_dump()}")
    c.add(
        report.usage == tally.usage,
        "ledger totals equal the summed API usage",
        f"ledger {report.usage.model_dump()} vs api {tally.usage.model_dump()}",
    )
    if price:
        c.add(
            report.total_cost > 0, "cost is priced from pricing.toml", f"${report.total_cost:.4f}"
        )
    else:
        c.add(None, "cost is priced from pricing.toml", "model not in pricing.toml")

    # --- rewind ---------------------------------------------------------------------------
    (root / "mine.txt").write_text("a file the agent never touched\n")
    restored = cli.rewind_code(session.log.state, root, n1)
    c.add(
        (root / "mine.txt").exists(),
        "rewind leaves files the agent never touched alone",
    )
    c.add(
        "app.py" in restored and (root / "app.py").read_text() == FILES["app.py"],
        "rewinding to before the fix restores app.py",
        ", ".join(restored),
    )

    # --- summary ---------------------------------------------------------------------------
    print(f"\n{BOLD}{len(c.rows)} checks, {c.failed} failed{RESET}")
    for state, name, detail in c.rows:
        if state == "FAIL":
            print(f"  {RED}FAIL{RESET} {name}: {detail}")
    print(f"{DIM}session log: {session.log.path}{RESET}")
    if made_temp and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"{DIM}kept: {root}{RESET}")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
