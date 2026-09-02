"""Path summaries for `leaf+summary` grafts, generated lazily by a cheap model."""

from __future__ import annotations

from kaipi import context, graph
from kaipi.model import SummaryGenerated
from kaipi.providers import Provider
from kaipi.store import Log

PROMPT = (
    "Summarise this exploration in under 150 words: what was tried, what was learned, "
    "what the final conclusion is. Keep concrete names (files, commands, errors). "
    "Output the summary only."
)


def ensure_summary(log: Log, provider: Provider, node_id: str, leaf_id: str | None) -> str:
    node = log.state.nodes[node_id]
    if node.summary is not None:
        return node.summary
    top = graph.lca(log.state, node_id, leaf_id) if leaf_id else None
    path = graph.lineage(log.state, node_id)
    if top is not None:
        path = path[[n.id for n in path].index(top) + 1 :]
    body = "\n\n".join(
        f"[user]\n{context.user_input(n)}\n[assistant]\n{context.final_answer(n)}" for n in path
    )
    reply = provider.complete(
        PROMPT, [{"role": "user", "content": [{"type": "text", "text": body}]}], []
    )
    text = "\n".join(b["text"] for b in reply.content if b.get("type") == "text").strip()
    log.append(SummaryGenerated(node_id=node_id, summary=text))
    return text
