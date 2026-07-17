from __future__ import annotations

from dataclasses import dataclass, field

from ..types import LayerReport, MemoryContextSummary
from .curated import CuratedStore
from .semantic import SemanticIndex
from .state import ConversationStateStore
from .summary import TurnSummaryStore
from .transcript import TranscriptStore


@dataclass
class MemoryFacade:
    transcript: TranscriptStore
    curated: CuratedStore
    state: ConversationStateStore
    summaries: TurnSummaryStore
    semantic: SemanticIndex
    recent_limit: int = 4

    def build_context(self, *, session_id: str, query: str) -> tuple[str, MemoryContextSummary]:
        layers: list[LayerReport] = []
        blocks: list[str] = []

        state_text, entity_count = self.state.render(session_id)
        if state_text:
            blocks.append(state_text)
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="used",
                    token_chars=len(state_text),
                    item_ids=[f"entities:{entity_count}"],
                )
            )
        else:
            layers.append(LayerReport(name="conversation_state", status="skipped"))

        curated_text, curated_count = self.curated.snapshot_text(session_id=session_id)
        if curated_text:
            blocks.append(curated_text)
            layers.append(
                LayerReport(
                    name="curated",
                    status="used",
                    token_chars=len(curated_text),
                    item_ids=[f"count:{curated_count}"],
                )
            )
        else:
            layers.append(LayerReport(name="curated", status="skipped"))

        summary_text = self.summaries.render(session_id, limit=8)
        if summary_text:
            blocks.append(summary_text)
            layers.append(
                LayerReport(name="turn_summary", status="used", token_chars=len(summary_text))
            )
        else:
            layers.append(LayerReport(name="turn_summary", status="skipped"))

        hits = self.semantic.search(session_id=session_id, query=query, limit=5)
        semantic_text = self.semantic.render(hits)
        if semantic_text:
            blocks.append(semantic_text)
            layers.append(
                LayerReport(
                    name="semantic",
                    status="used",
                    token_chars=len(semantic_text),
                    item_ids=[h["turn_id"] for h in hits],
                )
            )
        else:
            layers.append(LayerReport(name="semantic", status="skipped"))

        recent = self.transcript.recent_messages()
        # recent_messages already returns role/content pairs; render raw block for attention
        if recent:
            raw_lines = ["[RECENT_RAW]"]
            for msg in recent[-self.recent_limit * 2 :]:
                raw_lines.append(f"{msg['role']}: {msg['content'][:1000]}")
            raw_text = "\n".join(raw_lines)
            # avoid duplicating full raw into system if also injected as chat history —
            # still include a compact marker count.
            layers.append(
                LayerReport(
                    name="recent_raw",
                    status="used",
                    token_chars=sum(len(m["content"]) for m in recent),
                    item_ids=[str(i) for i in range(len(recent))],
                )
            )
        else:
            layers.append(LayerReport(name="recent_raw", status="skipped"))
            raw_text = ""

        # Prefer state/curated/historical in system memory block; recent is chat history.
        memory_system = "\n\n".join(b for b in blocks if b)
        summary = MemoryContextSummary(
            layers=layers,
            curated_count=curated_count,
            state_entity_count=entity_count,
            recent_turn_count=len(recent) // 2,
        )
        return memory_system, summary
