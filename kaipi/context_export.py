"""A local Markdown view of the next turn's assembled context."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kaipi import context, graph

if TYPE_CHECKING:
    from kaipi.cli import Session


def fence(text: str, language: str) -> str:
    delimiter = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", text)), default=0))
    return f"{delimiter}{language}\n{text}\n{delimiter}"


def export(s: Session, placeholder: str) -> Path:
    state = s.log.state
    ctx = context.build(
        state, s.leaf, s.pending, placeholder, guard=graph.is_exploration(state, s.leaf)
    )
    lineage = graph.live_lineage(state, s.leaf) if s.leaf else []
    rows = [
        "# kaipi 上下文检查",
        f"- 会话：{state.name or '未命名'} · `{s.log.path.stem}`",
        f"- 下一轮模型：`{s.wanted_model()}`",
        f"- 当前游标：`{s.name(s.leaf)}`",
        "- 发送路径：" + (" → ".join(s.name(n.id) for n in lineage) or "root（尚无历史消息）"),
        f"- 路径外节点：{len(state.nodes) - len(lineage)} 个，不会自动加载其内容。",
        "",
        "下方由实际请求使用的 context.build 组装，按发送顺序完整导出；本命令不调用模型。",
        f"下一条问题尚未输入，以 {placeholder} 占位；新问题及附件会在发送时加入。",
        "这是协议转换前的统一上下文，提供商适配层仍会转换请求格式。",
        "历史中的嫁接快照按原样保留；本轮待嫁接内容另列如下，不隐式混入其他分支。",
        "",
        "## 本轮待嫁接",
    ]
    rows += [f"- {s.name(g.src_id)} · `{g.depth}`" for g in ctx.grafts] or ["无。"]
    if any(g.depth == "leaf+summary" and not state.nodes[g.src_id].summary for g in ctx.grafts):
        rows.append("部分嫁接摘要尚未生成；此处保留占位，实际发送前才会生成摘要。")
    rows += ["", "## System", fence(ctx.system, "text"), "", "## 消息（按发送顺序）"]
    sources = [s.name(n.id) for n in lineage for _ in n.payload]
    for i, message in enumerate(ctx.messages):
        source = sources[i] if i < ctx.payload_start else "本轮待发送"
        rows += [
            "",
            f"### {i + 1}. {message['role']} · {source}",
            fence(json.dumps(message, ensure_ascii=False, indent=2), "json"),
        ]
        if i in ctx.cache_points:
            rows.append("缓存断点：本条消息末尾。")
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", state.name or "未命名").strip(" .")
    title = title.encode("utf-8")[:120].decode("utf-8", errors="ignore") or "未命名"
    stem = f"{datetime.now():%Y%m%d%H%M%S}-{title}-{s.name(s.leaf)}-context"
    index = 1
    while True:
        path = s.cwd / f"{stem}{f'-{index}' if index > 1 else ''}.md"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            index += 1
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n\n".join(rows) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.resolve()
