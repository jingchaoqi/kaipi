"""Provider/model configuration prompts; changes are saved only after input completes."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx
import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import Validator
from prompt_toolkit.widgets import Label, RadioList

from kaipi import catalog, i18n, ledger, settings, update
from kaipi.display import columns
from kaipi.i18n import tr

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def statusline() -> None:
    if not sys.stdin.isatty():
        typer.echo(tr("请在交互终端中运行 kaipi statusline，或在对话中使用 /statusline"))
        return
    values = settings.statusline()
    notice = tr("选择底部状态行要显示的元素")

    def label(index: int) -> str:
        return columns(
            [
                (tr(title), tr(settings.STATUSLINE_OPTIONS[key][values[key]]))
                for key, title in settings.STATUSLINE_ITEMS.items()
            ]
        )[index]

    choices = RadioList(
        [(key, partial(label, i)) for i, key in enumerate(settings.STATUSLINE_ITEMS)],
        select_on_focus=True,
        open_character="",
        select_character="▸",
        close_character="",
        show_cursor=False,
    )
    keys = KeyBindings()

    @keys.add("left")
    @keys.add("right")
    def change(event: Any) -> None:
        key = choices.current_value
        options = list(settings.STATUSLINE_OPTIONS[key])
        step = 1 if event.key_sequence[-1].key == "right" else -1
        values[key] = options[(options.index(values[key]) + step) % len(options)]

    @keys.add("escape", eager=True)
    def save_and_back(event: Any) -> None:
        nonlocal notice
        try:
            settings.save({"statusline": values})
        except OSError:
            notice = tr("保存失败，尚未退出；请重试，或 Ctrl+C 取消修改")
            return
        event.app.exit()

    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event: Any) -> None:
        event.app.exit()

    app: Application[None] = Application(
        layout=Layout(
            HSplit(
                [
                    Label(tr("底部状态行")),
                    HSplit([choices], height=Dimension(max=8)),
                    Label(lambda: notice),
                    Label(tr("↑↓ 选择 · ←→ 切换选项 · Esc 保存返回 · Ctrl+C 取消")),
                ]
            ),
            focused_element=choices,
        ),
        key_bindings=keys,
        style=Style.from_dict({"radio-selected": "bg:#30343b fg:#f3f4f6 bold"}),
        erase_when_done=True,
    )
    app.ttimeoutlen = 0.1
    try:
        app.run()
    except (EOFError, KeyboardInterrupt):
        pass


def config() -> None:
    if not sys.stdin.isatty():
        typer.echo(tr("请在交互终端中运行 kaipi config，或在对话中使用 /config"))
        return
    notice = "修改立即保存并生效"

    def label(index: int) -> str:
        return columns(
            [
                (tr("每日自动检查更新"), tr("开") if settings.auto_check_updates() else tr("关")),
                (tr("语言"), i18n.LANGUAGES[i18n.language()]),
            ]
        )[index]

    choices = RadioList(
        [
            ("auto_check_updates", partial(label, 0)),
            ("language", partial(label, 1)),
        ],
        select_on_focus=True,
        open_character="",
        select_character="▸",
        close_character="",
        show_cursor=False,
    )
    keys = KeyBindings()

    @keys.add("left")
    @keys.add("right")
    def change(event: Any) -> None:
        nonlocal notice
        if choices.current_value == "language":
            options = list(i18n.LANGUAGES)
            step = 1 if event.key_sequence[-1].key == "right" else -1
            selected = options[(options.index(i18n.language()) + step) % len(options)]
            try:
                settings.save({"language": selected})
                notice = "修改立即保存并生效"
            except OSError:
                notice = "保存失败，设置未修改；请检查配置目录权限后重试"
            return
        enabled = event.key_sequence[-1].key == "right"
        if enabled == settings.auto_check_updates():
            return
        try:
            settings.set_auto_check_updates(enabled)
        except OSError:
            notice = "保存失败，设置未修改；请检查配置目录权限后重试"
            return
        notice = "修改立即保存并生效"
        if enabled:
            update.background(typer.echo)

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    @keys.add("c-d")
    def back(event: Any) -> None:
        event.app.exit()

    app: Application[None] = Application(
        layout=Layout(
            HSplit(
                [
                    Label(lambda: tr("kaipi 设置")),
                    choices,
                    Label(lambda: tr(notice)),
                    Label(lambda: tr("↑↓ 选择 · ←→ 修改 · 无需回车 · Esc 返回")),
                ]
            ),
            focused_element=choices,
        ),
        key_bindings=keys,
        style=Style.from_dict({"radio-selected": "bg:#30343b fg:#f3f4f6 bold"}),
        erase_when_done=True,
    )
    app.ttimeoutlen = 0.1
    with patch_stdout(raw=True):
        try:
            app.run()
        except (EOFError, KeyboardInterrupt):
            pass


def prompt(label: str, **kwargs: Any) -> Any:
    if not sys.stdin.isatty():
        # Also support cancellation in plain/piped mode.
        value = typer.prompt(label, **{**kwargs, "type": str})
        if "\x1b" in value:
            raise KeyboardInterrupt
    else:
        keys = KeyBindings()

        @keys.add("escape")
        def cancel(event: Any) -> None:
            event.app.exit(exception=KeyboardInterrupt())

        session: PromptSession[str] = PromptSession(key_bindings=keys)
        session.app.ttimeoutlen = 0.1
        default = kwargs.get("default")
        suffix = f" [{default}]" if default and kwargs.get("show_default", True) else ""
        value = (
            session.prompt(f"{label}{suffix}: ", is_password=kwargs.get("hide_input", False))
            or default
            or ""
        )
    try:
        return kwargs.get("type", str)(value)
    except ValueError as e:
        raise typer.BadParameter(tr("请输入有效序号")) from e


def run(command: Callable[[], None]) -> bool:
    typer.echo(tr("{DIM}Esc 退出当前命令，不修改任何配置{RESET}", DIM=DIM, RESET=RESET))
    try:
        command()
    except (EOFError, KeyboardInterrupt, typer.Abort):
        typer.echo(tr("{DIM}已取消，配置未修改{RESET}", DIM=DIM, RESET=RESET))
        return False
    return True


def choose(rows: list[str], current: int = 0, *, title: str = "选择模型", hint: str = "") -> int:
    title = tr(title)
    if not sys.stdin.isatty():
        typer.echo(f"{BOLD}{title}{RESET}")
        for i, row in enumerate(rows, 1):
            typer.echo(f"  {i:>2}. {row}")
        pick = (
            int(prompt("序号 / Number" if hint else tr("序号"), type=int, default=current + 1)) - 1
        )
        if not 0 <= pick < len(rows):
            raise typer.BadParameter(tr("序号要在 1..{v0} 之间", v0=len(rows)))
        return pick
    choices = RadioList(
        [(i, ANSI(row)) for i, row in enumerate(rows)],
        default=current,
        select_on_focus=True,
        open_character="",
        select_character="▸",
        close_character="",
        show_cursor=False,
    )
    keys = KeyBindings()

    @keys.add("enter", eager=True)
    def accept(event: Any) -> None:
        event.app.exit(result=choices.current_value)

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    app: Application[int] = Application(
        layout=Layout(
            HSplit(
                [
                    Label(title),
                    HSplit([choices], height=Dimension(max=10)),
                    Label(hint or tr("↑↓ 选择 · Enter 确认 · Esc 退出，不修改配置")),
                ]
            ),
            focused_element=choices,
        ),
        key_bindings=keys,
        style=Style.from_dict({"radio-selected": "bg:#30343b fg:#f3f4f6 bold"}),
        erase_when_done=True,
    )
    app.ttimeoutlen = 0.1
    return app.run()


def choose_provider(rows: list[str]) -> int:
    if not sys.stdin.isatty():
        typer.echo(tr("{BOLD}选择 API 提供商{RESET}", BOLD=BOLD, RESET=RESET))
        for i, row in enumerate(rows, 1):
            typer.echo(f"  {i:>2}. {row}")
        pick = int(prompt(tr("序号"), type=int)) - 1
        if not 0 <= pick < len(rows):
            raise typer.BadParameter(tr("序号要在 1..{v0} 之间", v0=len(rows)))
        return pick

    def selected(text: str) -> int | None:
        try:
            pick = int(text) - 1
            return pick if 0 <= pick < len(rows) else None
        except ValueError:
            return None

    keys = KeyBindings()

    @keys.add("up")
    @keys.add("down")
    def move(event: Any) -> None:
        buffer = event.current_buffer
        index = selected(buffer.text)
        down = event.key_sequence[-1].key == "down"
        if index is None:
            index = 0 if down else len(rows) - 1
        else:
            index = max(0, min(len(rows) - 1, index + (1 if down else -1)))
        buffer.text = str(index + 1)
        buffer.cursor_position = len(buffer.text)

    @keys.add("escape", eager=True)
    def cancel(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    session: PromptSession[str] = PromptSession(
        tr("序号："),
        key_bindings=keys,
        validator=Validator.from_callable(
            lambda text: selected(text) is not None,
            error_message=tr("请输入 1..{v0} 的序号，或用上下键选择", v0=len(rows)),
            move_cursor_to_end=True,
        ),
        validate_while_typing=False,
        erase_when_done=True,
        style=Style.from_dict({"cursor-line": "bg:#30343b fg:#f3f4f6"}),
    )

    def content() -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        index = selected(session.default_buffer.text)
        for i, row in enumerate(rows):
            fragments.append(("", f"{'▸' if i == index else ' '} {i + 1:>2}. "))
            fragments.extend(to_formatted_text(ANSI(row)))
            if i < len(rows) - 1:
                fragments.append(("", "\n"))
        return fragments

    menu = Window(
        FormattedTextControl(
            content,
            get_cursor_position=lambda: Point(0, selected(session.default_buffer.text) or 0),
            show_cursor=False,
        ),
        height=Dimension(max=10),
        dont_extend_height=True,
        cursorline=Condition(lambda: selected(session.default_buffer.text) is not None),
    )
    session.layout.current_window.dont_extend_height = Always()
    session.layout.container = HSplit(
        [
            Label(tr("选择 API 提供商")),
            menu,
            session.layout.container,
            Label(tr("输入序号或 ↑↓ 选择 · Enter 确认 · Esc 取消")),
        ]
    )
    session.app.ttimeoutlen = 0.1
    return int(session.prompt()) - 1


def provider(*, report: Callable[[str], None] = typer.echo) -> None:
    """Pick a vendor, confirm its endpoint, paste a key: the three things that otherwise
    have to be exported by hand every session. Regional endpoints differ (Moonshot and
    Z.ai each have a .cn and an international one), so the URL is always editable."""
    from kaipi.providers import BUILTIN, auth, parse_models, remove_auth, save_auth

    current = auth()
    done = current.get("providers", {})
    if done:
        typer.echo(tr("{BOLD}已配置的提供商{RESET}", BOLD=BOLD, RESET=RESET))
        for row in columns(
            [
                (
                    n,
                    f"{DIM}{saved.get('base_url', '')}{RESET}",
                    f"{DIM}{', '.join(saved.get('models', [])) or tr('(还没填模型)')}{RESET}",
                )
                for n, saved in done.items()
            ]
        ):
            typer.echo(f"   · {row}")
        typer.echo(
            tr(
                "{DIM}下面选一个即可新增或修改；切换用哪个模型是 /model 的事{RESET}",
                DIM=DIM,
                RESET=RESET,
            )
        )

    names = list(BUILTIN)
    rows = []
    for n in names:
        cfg = BUILTIN[n]
        mark = tr(" (已配置)").strip() if n in done else ""
        url = cfg.base_url or tr("(官方默认)")
        rows.append((n, f"{DIM}{cfg.api}{RESET}", f"{DIM}{url}{RESET}", mark))
    name = names[choose_provider(columns(rows))]
    cfg = BUILTIN[name]

    saved = done.get(name, {})
    if saved:
        action = choose(
            [
                tr("刷新模型与价格（保留地址和 key）"),
                tr("编辑配置"),
                tr("清除配置（移除地址、key 和模型列表）"),
            ],
            title=tr("{name} · 已配置", name=name),
        )
        if action == 0:
            refresh(name, saved.get("models", []), report=report)
            return
        if action == 2:
            was_active = current.get("provider") == name or str(
                current.get("model", "")
            ).startswith(name + "/")
            try:
                remove_auth(name)
            except OSError:
                report(
                    tr(
                        "{RED}清除失败，原配置已保留；请检查配置目录权限{RESET}",
                        RED=RED,
                        RESET=RESET,
                    )
                )
                return
            report(tr("{GREEN}{name} 的配置已清除{RESET}", GREEN=GREEN, name=name, RESET=RESET))
            if was_active:
                report(tr("当前模型选择已清除，请用 /model 选择其他模型，或 /provider 添加配置"))
            return
    base = prompt(
        tr("API 地址（不同地区可能不同，回车接受）"),
        default=saved.get("base_url") or cfg.base_url or "https://api.anthropic.com",
    )
    saved_key = saved.get("api_key", "")
    key = prompt(
        tr("{v0} 的 key", v0=cfg.api_key_env) + (tr("（回车保留已存的）") if saved_key else ""),
        hide_input=True,
        default="" if saved_key else None,
        show_default=False,
    ).strip()
    known = [
        m.removeprefix(f"{pr.provider}/")
        for m, pr in ledger.Pricing.load().models.items()
        if pr.provider in {name, cfg.model_catalog}
    ]
    if known:
        typer.echo(
            tr(
                "{DIM}价格表里这个提供商的模型：{v1}{RESET}",
                DIM=DIM,
                v1=", ".join(known),
                RESET=RESET,
            )
        )
    defaults = known or saved.get("models", [])
    listed = prompt(
        tr("你要用的 model id（回车全部接受；只选部分请手动输入，逗号分隔）")
        if defaults
        else tr("你要用的 model id（请手动输入，逗号分隔）"),
        default=", ".join(defaults),
    )
    picked = parse_models(listed)

    path = save_auth(name, base.strip().rstrip("/"), key, picked)
    report(
        tr(
            "{GREEN}{name} 已保存到 {path}（权限 600，只有你能读）{RESET}",
            GREEN=GREEN,
            name=name,
            path=path,
            RESET=RESET,
        )
    )
    typer.echo(
        tr(
            "{DIM}临时想换 key，export {v1} 仍然优先于这里{RESET}",
            DIM=DIM,
            v1=cfg.api_key_env,
            RESET=RESET,
        )
    )
    typer.echo(tr("{DIM}接下来用 /model 选一个模型启用{RESET}", DIM=DIM, RESET=RESET))


def model(*, report: Callable[[str], None] = typer.echo) -> None:
    """Switch models across every provider you configured. One list, so moving from a
    Kimi model to a Claude one is the same gesture as moving between two Kimi ones."""
    from kaipi.providers import active_model, configured_models, use_model

    pairs = configured_models()
    if not pairs:
        report(
            tr(
                "{RED}还没有可选的模型。先跑 /provider 配置一个提供商并列出它的 model id{RESET}",
                RED=RED,
                RESET=RESET,
            )
        )
        return
    now, pricing = active_model(), ledger.Pricing.load()
    rows, current = [], 0
    for i, (who, m) in enumerate(pairs):
        spec = f"{who}/{m}"
        price = pricing.find(spec)
        cost = (
            tr("in {v0}/out {v1} 每百万", v0=price.input, v1=price.output)
            if price
            else tr("不在价格表，账本按 0 记")
        )
        mark = f"{GREEN}{tr(' (当前)').strip()}{RESET}" if spec == now else ""
        rows.append((m, f"{DIM}{who}{RESET}", f"{DIM}{cost}{RESET}", mark))
        if spec == now:
            current = i
    who, model = pairs[choose(columns(rows), current)]
    use_model(who, model)
    report(
        tr(
            "{GREEN}已启用 {model}（{who}），下一句开始生效{RESET}",
            GREEN=GREEN,
            model=model,
            who=who,
            RESET=RESET,
        )
    )


def refresh(
    name: str, saved_models: list[str], *, report: Callable[[str], None] = typer.echo
) -> None:
    typer.echo(tr("{DIM}正在读取 kaipi 在线模型与价格目录…{RESET}", DIM=DIM, RESET=RESET))
    try:
        prices, ids = catalog.preview(name, saved_models)
    except (OSError, ValueError, httpx.HTTPError):
        report(tr("{RED}刷新失败，原配置和价格已保留；请稍后重试{RESET}", RED=RED, RESET=RESET))
        return
    added = [m for m in ids if m not in saved_models]
    typer.echo(
        tr(
            "{BOLD}{name} · 目录内 {v2} 个模型 · 新增 {v3} 个{RESET}",
            BOLD=BOLD,
            name=name,
            v2=len(prices),
            v3=len(added),
            RESET=RESET,
        )
    )
    for row in columns(
        [
            (
                key.removeprefix(f"{price.provider}/"),
                DIM + tr("in {v0}/out {v1} 每百万", v0=price.input, v1=price.output) + RESET,
            )
            for key, price in prices.items()
        ]
    ):
        typer.echo(f"  {row}")
    typer.echo(
        tr("{DIM}保留自定义模型和价格；保存后可在 /model 中选择{RESET}", DIM=DIM, RESET=RESET)
    )
    prompt(tr("回车保存刷新结果；Esc 取消"), default="", show_default=False)
    try:
        catalog.save(name, ids, prices)
    except OSError:
        report(tr("{RED}保存失败，原配置和价格已保留{RESET}", RED=RED, RESET=RESET))
        return
    report(
        tr(
            "{GREEN}{name} 模型与价格已刷新，/model 可立即使用{RESET}",
            GREEN=GREEN,
            name=name,
            RESET=RESET,
        )
    )
