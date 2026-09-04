"""Path summaries for `leaf+summary` grafts, generated lazily by a cheap model."""

from __future__ import annotations

from kaipi import context, graph, model
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
    path = graph.path_below_lca(log.state, node_id, leaf_id)
    body = "\n\n".join(context.leaf_text(n) for n in path)
    reply = provider.complete(
        PROMPT, [{"role": "user", "content": [{"type": "text", "text": body}]}], []
    )
    text = model.text_of(reply.content).strip()
    log.append(
        SummaryGenerated(node_id=node_id, summary=text, model=provider.model, usage=reply.usage)
    )
    return text
