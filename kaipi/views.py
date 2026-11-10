"""Shared text presentation and slash syntax. All mutations belong to core."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from prompt_toolkit.formatted_text import ANSI, fragment_list_width, to_formatted_text

from kaipi import context, core, graph, ledger, settings, update
from kaipi.core import Session, rewind_paths, session_title
from kaipi.display import columns
from kaipi.i18n import tr
from kaipi.model import Depth, blocks, text_of
from kaipi.store import Log, list_sessions

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
PLACEHOLDER = "「您即将输入的下一句」"


def color(c: ledger.Color, text: str) -> str:
    return f"{GREEN if c == 'green' else RED}{text}{RESET}"


def plain(text: str) -> str:
    return "".join(fragment[1] for fragment in to_formatted_text(ANSI(text)))


def burden_line(s: Session) -> str:
    return tr("trunk burden: {v0}", v0=ledger.fmt(ledger.trunk_burden(s.log.state)))


def turn_line(s: Session, node_id: str, *, exploration: bool | None = None) -> str:
    """Format a turn's saved usage; replay shows its own context, not today's trunk."""
    n = s.log.state.nodes[node_id]
    replay = exploration is None
    if replay:
        exploration = not graph.on_trunk(s.log.state, node_id)
    return tr(
        "[{v0} {kind}] ${cost:.4f}  cache read {v3}/{v4}  {v5}",
        v0=s.name(node_id),
        kind=tr("ui.exploration" if exploration else "ui.trunk"),
        cost=s.pricing.price(n.model).cost(n.usage),
        v3=ledger.fmt(n.usage.cache_read),
        v4=ledger.fmt(n.usage.context),
        v5=tr("context {window}", window=ledger.fmt(n.context_tokens))
        if replay
        else burden_line(s),
    )


def status(s: Session, width: int = 120) -> str:
    """Fit the core's status figures to the terminal; the path yields first on narrow screens."""
    fields = settings.statusline()
    if all(value == "off" for value in fields.values()):
        return ""
    data = s.status_data()
    burden, limit = data["burden"], data["window"]
    window = f"{ledger.fmt(burden)}/{limit // 1000}k" if limit else ledger.fmt(burden)
    if fields["context"] == "percent":
        window = f"{burden / limit:.1%}" if limit else tr("未知")
    tokens = sum(item["tokens"] for item in data["saved"].values())
    money = sum(item["cost"] for item in data["saved"].values())
    spent = "  ".join(
        text
        for key, text in (
            ("context", tr("context {window}", window=window)),
            ("spent", tr("spent ${v0:.4f}", v0=data["cost"])),
        )
        if fields[key] != "off"
    )
    figures = []
    if fields["saved_tokens"] != "off":
        figures.append(f"{ledger.fmt(tokens)}/{tr('每轮')}")
    if fields["saved_cost"] != "off":
        figures.append(f"${money:.4f}")
    long = tr("saved ") + " = ".join(figures) if figures else ""
    short_ = tr("saved ${money:.4f}", money=money) if fields["saved_cost"] != "off" else long

    def bar(where: str, figure: str) -> str:
        head = "/".join(data[key] for key in ("provider", "model") if fields[key] != "off")
        return "  ".join(
            part
            for part in (
                head,
                f"{DIM}{where}{RESET}" if where else "",
                spent,
                f"{GREEN}{figure}{RESET}" if figure else "",
            )
            if part
        )

    def visible(text: str) -> int:
        return fragment_list_width(to_formatted_text(ANSI(text)))

    # Narrower than everything fits: give up the path first, then the per-turn figure.
    # A number that is not on screen is worth less than a path that is not.
    name = s.log.state.name if fields["session"] != "off" else ""
    available = width - visible(bar(name, long)) - (3 if name else 2)
    directory = ""
    if fields["directory"] == "full":
        directory = _home(s.cwd, max(12, available))
    elif fields["directory"] == "compact":
        directory = s.cwd.name or s.cwd.anchor
    for where, figure in (
        (" · ".join(part for part in (name, directory) if part), long),
        (name, long),
        ("", long),
        ("", short_),
    ):
        out = bar(where, figure)
        if visible(out) <= width:
            return out
    return bar("", short_)


COMMANDS: list[tuple[str, str, str]] = [
    ("tree", "", "会话树导航：方向键选择节点，回车从此继续"),
    ("raw", "", "原样打印下一轮要发给模型的全部内容，含嫁接块；不发送、不花钱"),
    ("inspect-context", "", "导出下一轮上下文为 Markdown，在 workspace 根目录查看"),
    ("explore", "<你的问题>", "从当前位置岔出去问一句，主干钉在原地不动"),
    ("go", "[节点id]", "跳转到节点；不带参数打开会话树导航"),
    (
        "graft",
        "<节点id> [--depth leaf|leaf+summary|branch] [--with-tool <id>...]",
        "把那个节点嫁接进下一句输入，只搬结论",
    ),
    ("archive", "<节点id>", "把节点连同整棵子树收起来，退出上下文"),
    ("restore", "<节点id>", "撤销归档"),
    ("trunk", "[pin <节点id>]", "显示主干，或把主干钉到某个节点"),
    ("rewind", "[节点id] [both|code|conversation]", "回退：上下键选节点，再选回退方式"),
    ("status", "", "当前状态：模型、会话、上下文、用量与费用"),
    ("version", "", "显示当前 kaipi 版本"),
    ("update", "", "检查并安装新版，完成后接回当前会话"),
    ("ledger", "", "总账：花费、各项 token、探索/嫁接/打断各省了多少"),
    ("canvas", "", "把会话搬到浏览器（画布里敲 /cli 搬回来）"),
    ("provider", "", "配置 API 提供商：地址、key、模型 id"),
    ("model", "", "换一个模型，下一句开始生效"),
    ("config", "", "设置：上下选择、左右修改，立即生效，Esc 返回"),
    ("statusline", "", "定制底部状态行：上下选择、左右切换选项，Esc 保存返回"),
    ("new", "", "结束当前对话、清空显示，进入新对话；旧对话保留"),
    ("resume", "[对话id]", "接上旧对话；不带参数用上下键选择，回车进入"),
    ("rename", "<名字>", "给当前对话起个名字"),
    ("retry", "", "重新运行最新一轮，保留原始输入，不带入旧回复"),
    ("help", "", "显示这份清单"),
    ("quit", "", "结束（/exit 也一样）"),
]
NODE_ARG = {"go", "graft", "archive", "restore", "rewind"}  # first argument is a node id

HELP = "🌱 欢迎使用 kaipi，输入 /help 查看所有指令及用法。"


def short(node_id: str) -> str:
    return node_id[-6:]


def _home(path: Path, limit: int = 34) -> str:
    """The working directory for the status bar. Shortened under $HOME, and elided from the
    left past `limit`: the numbers to its right are the point of the bar, and a deep path -
    a macOS $TMPDIR is 60 characters before it says anything - would push them off screen."""
    try:
        out = "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        out = str(path)
    if len(out) <= limit:
        return out
    parts = out.split("/")
    for keep in range(2, len(parts)):  # widen from the tail until it no longer fits
        tail = "…/" + "/".join(parts[-keep:])
        if len(tail) > limit:
            return "…/" + "/".join(parts[-(keep - 1) :])
    return out


def delta_line(before: int, after: int) -> str:
    sign = "+" if after >= before else "-"
    return tr(
        "主干上下文：{before} → {after}（{change}）",
        before=ledger.fmt(before),
        after=ledger.fmt(after),
        change=sign + ledger.fmt(abs(after - before)),
    )


def tree_rows(
    s: Session, *, compact: bool = False, live_only: bool = False
) -> list[tuple[str, str]]:
    """Tree order and connectors shared by static output and interactive navigation."""
    st = s.log.state
    trunk = graph.trunk(st)
    trunk_ids = {n.id for n in graph.lineage(st, trunk)} if trunk else set()
    out: list[tuple[str, str]] = []

    def walk(parent: str | None, prefix: str) -> None:
        children = graph.children(st, parent, live_only=live_only)
        for i, n in enumerate(children):
            last = i == len(children) - 1
            branch = "└─ " if last else "├─ "
            status = {
                "archived": tr(" [已归档]"),
                "tombstone": tr(" [未完成]"),
                "aborted": tr(" {RED}[已废弃 · 不进上下文]{RESET}", RED=RED, RESET=RESET),
            }.get(n.status, "")
            tags = (tr(" · 当前") if n.id == s.leaf else "") + (
                tr(" · 主线") if n.id in trunk_ids else ""
            )
            label = " ".join(context.user_input(n).split())[:64]
            line = f"{prefix}{branch}{s.name(n.id)}{tags}  {label}{status}"
            if not compact:
                own = ledger.fmt(ledger.own_tokens(st, n.id))
                cost = s.pricing.price(n.model).cost(n.usage)
                graft = f"  ⟵{','.join(s.name(g.src_id) for g in n.grafts)}" if n.grafts else ""
                line += f" {DIM}{own} tokens ${cost:.4f}{graft}{RESET}"
            out.append((n.id, line if n.status == "live" else f"{DIM}{line}{RESET}"))
            walk(n.id, prefix + ("   " if last else "│  "))

    walk(None, "")
    return out


def cmd_tree(s: Session) -> list[str]:
    out = [line for _, line in tree_rows(s)]
    if s.pending:  # staged but not used yet: no node carries them until the next input
        staged = "  ".join(f"{s.name(e.src_id)}({e.depth})" for e in s.pending)
        out.append(
            tr(
                "{DIM}已登记待嫁接（下一句生效）：{staged}{RESET}",
                DIM=DIM,
                staged=staged,
                RESET=RESET,
            )
        )
    out.append(
        tr("{DIM}主线 · 当前  ⟵嫁接  |  {v1}{RESET}", DIM=DIM, v1=burden_line(s), RESET=RESET)
    )
    return out


def cmd_raw(s: Session) -> list[str]:
    """Exactly what the next turn would send, assembled by the same `context.build` the agent
    uses (§4), with the input you have not typed yet standing in for itself. It sends
    nothing: the size is `ledger.estimate_tokens`, not the provider's counter, which is an
    HTTP round trip on two of the four protocols. So a `leaf+summary` graft whose summary
    does not exist yet shows `(no summary available)` here and gets one when you really
    send."""
    st = s.log.state
    ctx = s.inspect_context(PLACEHOLDER)
    out = [f"{DIM}=== system prompt ==={RESET}", ctx.system]
    out.append(
        tr(
            "{DIM}--- ↑ 缓存断点（system prompt 末尾，Anthropic 系协议固定放这一个）---{RESET}",
            DIM=DIM,
            RESET=RESET,
        )
    )
    total = ledger.estimate_tokens(ctx.system)
    for i, m in enumerate(ctx.messages):
        out.append(
            tr(
                "{DIM}=== 第 {v1}/{v2} 条消息 · {v3} ==={RESET}",
                DIM=DIM,
                v1=i + 1,
                v2=len(ctx.messages),
                v3=m["role"],
                RESET=RESET,
            )
        )
        for b in blocks(m["content"]):
            kind = str(b.get("type", ""))
            if kind == "tool_use":
                body = f"$ {b.get('input', {}).get('command', '')}"
            elif kind == "tool_result":
                body = text_of(b.get("content", ""))
            else:
                body = str(b.get("text", "")) or f"({kind})"
            out.append(f"{DIM}  [{kind}]{RESET}")
            out.append(body)
            total += ledger.estimate_tokens(body)  # every block, not just the text ones:
        if i in ctx.cache_points:  # a test log is most of what a real turn weighs
            out.append(
                tr(
                    "{DIM}--- ↑ 缓存断点（第 {v1} 条消息末尾）---{RESET}",
                    DIM=DIM,
                    v1=i + 1,
                    RESET=RESET,
                )
            )
    points = len(ctx.cache_points) + 1  # the fixed one on the system prompt is not in the set
    out.append(
        tr(
            "{DIM}=== {v1} 条消息  约 {v2} token（本地估算，没有发任何请求）"
            "  缓存断点 {points} 处 ==={RESET}",
            DIM=DIM,
            v1=len(ctx.messages),
            v2=ledger.fmt(total),
            points=points,
            RESET=RESET,
        )
    )
    stale = [
        g.src_id for g in ctx.grafts if g.depth == "leaf+summary" and not st.nodes[g.src_id].summary
    ]
    if stale:
        names = "、".join(s.name(i) for i in stale)
        out.append(
            tr(
                "{RED}{names} 的摘要还没生成，真正发送时才会生成{RESET}",
                RED=RED,
                names=names,
                RESET=RESET,
            )
        )
    return out


def cmd_go(s: Session, ref: str) -> list[str]:
    nid = s.go(ref)
    kind = tr("ui.trunk" if nid == graph.trunk(s.log.state) else "ui.exploration")
    return [tr("at {v0} ({kind}); next input opens a branch here", v0=s.name(nid), kind=kind)]


def cmd_graft(s: Session, ref: str, depth: Depth, with_tool: list[str]) -> list[str]:
    src = s.resolve(ref)
    st = s.log.state
    preview = s.graft(ref, depth, with_tool)
    before = ledger.trunk_burden(st)
    on_trunk = s.leaf == graph.trunk(st)
    out = [
        tr("graft {v0} -> next node  depth={depth}  ", v0=s.name(src), depth=depth)
        + "  ".join(f"{d}: +{ledger.fmt(n)}" for d, n in preview.items())
    ]
    after = before + preview[depth] if on_trunk else before
    out.append(color("green", delta_line(before, after)) + "  (" + tr("仅追加，缓存前缀不变") + ")")
    if with_tool:
        out.append(tr("including tool outputs: {v0}", v0=", ".join(with_tool)))
    return out


def cmd_archive(s: Session, ref: str) -> list[str]:
    out = _set_status(s, ref, restore=False)
    return [
        *out,
        tr(
            "{DIM}restore with /restore {v1}{RESET}",
            DIM=DIM,
            v1=s.name(s.resolve(ref)),
            RESET=RESET,
        ),
    ]


def cmd_restore(s: Session, ref: str) -> list[str]:
    return _set_status(s, ref, restore=True)


def _set_status(s: Session, ref: str, *, restore: bool) -> list[str]:
    """Archive or restore a whole subtree (one atomic operation, §2.1) and price it."""
    nid = s.resolve(ref)
    before = ledger.trunk_burden(s.log.state)
    ids = s.set_status(ref, restore=restore)
    word = "restored" if restore else "archived"
    c, _ = ledger.cache_color(s.log.state)
    why = tr("缓存前缀改变，需重写缓存并丢弃思考块" if c == "red" else "仅追加，缓存前缀不变")
    return [
        tr("{word} {v1} node(s) under {v2}", word=tr("ui." + word), v1=len(ids), v2=s.name(nid)),
        color(c, delta_line(before, ledger.trunk_burden(s.log.state))) + f"  ({why})",
    ]


def cmd_trunk(s: Session, ref: str | None) -> list[str]:
    st = s.log.state
    if ref is None:
        pin = st.trunk_pin
        heur = graph.trunk(st.model_copy(update={"trunk_pin": None}))
        return [
            tr(
                "主干：{node}  固定：{pin}  自动选择：{heuristic}",
                node=s.name(graph.trunk(st)),
                pin=s.name(pin) if pin else tr("ui.no"),
                heuristic=s.name(heur) if heur else "-",
            )
        ]

    nid = s.resolve(ref)
    before = ledger.trunk_burden(st)
    old_leaf = s.leaf
    s.pin(nid)
    out = [tr("trunk pinned to {v0}", v0=s.name(nid))]
    if old_leaf != s.leaf:
        # Pinning a branch means building on it; left behind, the next turn would be a
        # read-only exploration. Users tripped on that twice.
        out.append(tr("cursor moved to {v0}", v0=s.name(nid)))
    delta = delta_line(before, ledger.trunk_burden(s.log.state))
    return [*out, color("green", delta) + tr("  (no prefix changes)")]


def rewind_preview(s: Session, ref: str) -> str:
    nid = s.resolve(ref)
    paths = rewind_paths(s.log.state, nid)
    return tr(
        "files a code rewind to {v0} would restore: {v1}",
        v0=s.name(nid),
        v1=", ".join(paths) or tr("无"),
    )


def cmd_rewind(s: Session, ref: str, mode: str) -> list[str]:
    nid, done = s.rewind(ref, mode)
    out = []
    if mode in ("both", "code"):
        out.append(
            tr(
                "restored {v0} file(s) to {v1}; previous state at refs/kaipi/undo",
                v0=len(done),
                v1=s.name(nid),
            )
        )
    if mode in ("both", "conversation"):
        kind = tr("ui.trunk" if nid == graph.trunk(s.log.state) else "ui.exploration")
        out.append(
            tr("at {v0} ({kind}); next input opens a branch here", v0=s.name(nid), kind=kind)
        )
    return out


def cmd_status(s: Session) -> list[str]:
    """A local snapshot of this session; never build a provider or make an API call."""
    from importlib.metadata import version

    st, spec = s.log.state, s.wanted_model()
    r = ledger.report(st, s.pricing)
    price = s.pricing.find(spec)
    node = st.nodes.get(s.leaf or "")
    used = node.context_tokens if node and node.completed else None
    window = f"{price.context:,} tokens" if price and price.context else tr("未知")
    ctx = tr("尚无请求记录") if used is None else f"{used:,} tokens"
    ctx += tr(" / 窗口 {window}", window=window)
    if used is not None and price and price.context:
        ctx += f" · {used / price.context:.1%}"
    models = {n.model for n in st.nodes.values()} | {m for m, _ in st.summaries + st.titles}
    cost_note = (
        tr("部分模型未计价") if any(s.pricing.find(m) is None for m in models) else tr("价格表估算")
    )
    mode = tr("探索（只读）") if graph.is_exploration(st, s.leaf) else tr("主干续写")
    u = r.usage
    rows = [
        (tr("模型"), spec),
        (tr("提供商"), s.vendor(spec) or tr("未指定")),
        (tr("会话"), f"{st.name or tr('未命名')} · {st.session_id}"),
        (tr("目录"), str(s.cwd)),
        (
            tr("位置"),
            tr("{v0} · {mode} · 主干 {v2}", v0=s.name(s.leaf), mode=mode, v2=s.name(r.trunk)),
        ),
        (tr("上下文"), ctx),
        (tr("待嫁接"), tr("{v0} 个节点", v0=len(s.pending))),
        (tr("累计用量"), tr("输入 {v0:,} · 输出 {v1:,} tokens", v0=u.context, v1=u.output)),
        (
            tr("缓存读取"),
            f"{u.cache_read:,} tokens"
            + (tr(" · 命中 {v0:.1%}", v0=u.cache_read / u.context) if u.context else ""),
        ),
        (tr("会话费用"), f"${r.total_cost:.4f} ({cost_note})"),
    ]
    return [
        tr("\n{bold}kaipi {v1} · 状态{reset}\n", bold=BOLD, v1=version("kaipi"), reset=RESET),
        *(f"  {row}" for row in columns(rows)),
        tr(
            "\n  {dim}上下文为当前节点最近请求记录；待嫁接内容未计入。{reset}\n",
            dim=DIM,
            reset=RESET,
        ),
    ]


def cmd_ledger(s: Session) -> list[str]:
    r = ledger.report(s.log.state, s.pricing)
    u = r.usage
    out = [
        tr(
            "{BOLD}session {v1}  started on {v2}{RESET}",
            BOLD=BOLD,
            v1=s.log.path.stem,
            v2=s.log.state.model,
            RESET=RESET,
        ),
        tr(
            "total cost: ${v0:.4f}   tokens: uncached {v1}  cache write {v2}"
            "  cache read {v3}  output {v4}",
            v0=r.total_cost,
            v1=ledger.fmt(u.input_uncached),
            v2=ledger.fmt(u.cache_write),
            v3=ledger.fmt(u.cache_read),
            v4=ledger.fmt(u.output),
        ),
        tr(
            "trunk: {v0}   trunk burden: {v1}",
            v0=s.name(r.trunk) if r.trunk else "-",
            v1=ledger.fmt(r.trunk_burden),
        ),
    ]
    if r.branches:
        out.append(tr("explorations (one-time cost | tokens kept out of the trunk):"))
        out.extend(
            f"  {row}"
            for row in columns(
                [
                    (
                        s.name(b.leaf_id),
                        tr("ui." + b.status),
                        tr("{v0} 个节点", v0=b.nodes),
                        f"${b.one_time_cost:.4f}",
                        ledger.fmt(b.blocked_tokens),
                    )
                    for b in r.branches
                ]
            )
        )
    saved = ledger.saved_by_kind(s.log.state, s.pricing)
    labels = {
        "exploration": tr("探索分支未并入主干"),
        "graft": tr("嫁接带的是结论而非整条分支"),
        "interrupt": tr("打断的回合不进上下文"),
    }
    total_tok = sum(t for t, _ in saved.values())
    out.append(
        tr(
            "{GREEN}saved {v1} per trunk turn = ${v2:.4f} not spent so far{RESET}",
            GREEN=GREEN,
            v1=ledger.fmt(total_tok),
            v2=sum((c for _, c in saved.values())),
            RESET=RESET,
        )
    )
    for kind, (tok, money) in saved.items():
        if tok:
            out.append(
                tr(
                    "  {v0:>7}/turn  ${money:<8.4f} {DIM}{v3}{RESET}",
                    v0=ledger.fmt(tok),
                    money=money,
                    DIM=DIM,
                    v3=labels[kind],
                    RESET=RESET,
                )
            )
    dropped = sum(n.dropped_thinking for n in s.log.state.nodes.values())
    if dropped:
        out.append(
            tr(
                "{RED}thinking blocks dropped by the API (prefix edits): {dropped}{RESET}",
                RED=RED,
                dropped=dropped,
                RESET=RESET,
            )
        )
    return out


def session_rows(cwd: Path, current: str = "") -> list[tuple[Path, str]]:
    """(path, one line) per session, newest first."""
    rows = []
    for p in reversed(list_sessions(cwd)):
        st = Log(p).state
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        mark = tr(" (当前)") if p.name == current else ""
        rows.append(
            (
                p,
                tr(
                    "{v0}  {when}  {v2:>3} 轮  {v3}{mark}",
                    v0=short(p.stem),
                    when=when,
                    v2=len(st.nodes),
                    v3=session_title(st),
                    mark=mark,
                ),
            )
        )
    return rows


def cmd_new(s: Session) -> list[str]:
    s.new()
    return [tr("已开始新对话。输入 /resume 可找回之前的对话。")]


def cmd_resume(s: Session, ref: str) -> list[str]:
    """Pick up an earlier conversation in this directory."""
    if not s.resume(ref):
        return [tr("{DIM}已经在这个对话里{RESET}", DIM=DIM, RESET=RESET)]
    st = s.log.state
    return [
        tr(
            "{GREEN}接上 {v1}  {v2}{RESET}  {DIM}{v5} 轮，光标在 {v6}{v7}",
            GREEN=GREEN,
            v1=short(st.session_id),
            v2=session_title(st),
            RESET=RESET,
            DIM=DIM,
            v5=len(st.nodes),
            v6=s.name(s.leaf),
            v7=RESET,
        )
    ]


def cmd_rename(s: Session, name: str) -> list[str]:
    name = name.strip()
    s.rename(name)
    return [tr("{GREEN}这个对话现在叫「{name}」{RESET}", GREEN=GREEN, name=name, RESET=RESET)]


def cmd_help(s: Session) -> list[str]:
    rows = [
        (f"{BOLD}{f'/{name} {tr(args)}'.strip()}{RESET}", f"{DIM}{tr(what)}{RESET}")
        for name, args, what in COMMANDS
    ]
    out = [f"  {row}" for row in columns(rows)]
    return [
        *out,
        tr("  {DIM}Esc 停止当前回合 · 运行中敲的话会排队 · ↑↓ 翻历史{RESET}", DIM=DIM, RESET=RESET),
    ]


def verb(s: Session, argv: list[str]) -> list[str] | None:
    """A slash command either surface can run: what it has to say. None when it is not one
    of those - it asks something, runs a turn, or does not exist."""
    name, args = argv[0], argv[1:]
    if name in NODE_ARG and not args:
        usage = tr(next(a for n, a, _ in COMMANDS if n == name))
        # Interactive commands handle their picker before this shared verb.
        # Other surfaces still need an explicit node.
        usage = usage.replace(tr("[节点id]"), tr("<节点id>"), 1)
        raise ValueError(tr("用法：/{name} {usage}", name=name, usage=usage))
    match name:
        case "tree":
            return cmd_tree(s)
        case "raw":
            return cmd_raw(s)
        case "inspect-context":
            if args:
                raise ValueError(tr("用法：/inspect-context（不带参数）"))
            try:
                path = s.export_context(PLACEHOLDER)
            except OSError as e:
                raise ValueError(tr("无法导出上下文：{e}", e=e)) from e
            return [tr("上下文已导出。打开此文件查看：\n{path}", path=path)]
        case "go":
            return cmd_go(s, args[0])
        case "graft":
            depth: Depth = "leaf+summary"
            tools: list[str] = []
            if "--depth" in args:
                i = args.index("--depth")
                depth = args[i + 1]  # type: ignore[assignment]
                del args[i : i + 2]
            if "--with-tool" in args:
                i = args.index("--with-tool")
                tools, args = args[i + 1 :], args[:i]
            return cmd_graft(s, args[0], depth, tools)
        case "archive":
            return cmd_archive(s, args[0])
        case "restore":
            return cmd_restore(s, args[0])
        case "trunk":
            return cmd_trunk(s, args[1] if len(args) == 2 and args[0] == "pin" else None)
        case "ledger":
            return cmd_ledger(s)
        case "status":
            return cmd_status(s)
        case "version":
            return [f"kaipi {update.current()}"]
        case "new":
            if args:
                raise ValueError(tr("用法：/new（不带参数）"))
            return cmd_new(s)
        case "help":
            return cmd_help(s)
        case "resume" if args:
            return cmd_resume(s, args[0])
        case "rename":
            return cmd_rename(s, " ".join(args))
        case "rewind" if len(args) > 1:
            return cmd_rewind(s, args[0], args[1])
    return None


def event(s: Session, ev: core.Event) -> tuple[str, str] | None:
    """Render semantic core events; ordinary model/tool text is already display content."""
    if ev.kind == "completed":
        return "ledger", turn_line(s, ev.node_id, exploration=ev.exploration)
    if ev.kind == "aborted":
        n = s.log.state.nodes[ev.node_id]
        return "ledger", tr(
            "[{v0} 已废弃] ${cost:.4f}  不会进入之后的上下文",
            v0=s.name(n.id),
            cost=s.pricing.price(n.model).cost(n.usage),
        )
    if ev.kind == "summary_failed":
        return "ledger", tr(
            "[{v0} 摘要没生成：{why}] 改按 leaf 嫁接", v0=s.name(ev.node_id), why=ev.text
        )
    if ev.kind == "unpriced":
        return "ledger", tr(
            "{RED}warning: {model} not in pricing.toml; costs will read 0{RESET}",
            RED=RED,
            model=ev.text,
            RESET=RESET,
        )
    if ev.kind == "start":
        return None
    return ev.kind, ev.text
