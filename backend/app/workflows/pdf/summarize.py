"""Summarize a long document with bounded provider calls ("map, then reduce").

1. Map: summarize every chunk on its own (each call sees at most one chunk).
2. Reduce: join the chunk summaries one per line. If that fits in `combine_max_input_chars`, one
   "combine" call produces the final summary. Otherwise, pack the summaries into batches that fit,
   combine each batch, and repeat with the batch results until one summary remains.

Every call's input is bounded, and each round shrinks the work: a combined summary is at most
`final_max_chars`, which is at most half of `combine_max_input_chars`, so each batch holds at
least two inputs and the number of items at least halves every round.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.domain.enums import ProviderCallPurpose


@dataclass(frozen=True)
class SummarySettings:
    chunk_max_chars: int = 600
    final_max_chars: int = 2000
    combine_max_input_chars: int = 12000

    def __post_init__(self) -> None:
        # Every combine batch must fit at least two summaries plus the line break between them,
        # or a combine round can't reduce anything.
        if self.final_max_chars * 2 + 1 > self.combine_max_input_chars:
            raise ValueError(
                "combine_max_input_chars must be more than twice summary_final_max_chars"
            )
        if self.chunk_max_chars * 2 + 1 > self.combine_max_input_chars:
            raise ValueError(
                "combine_max_input_chars must be more than twice summary_chunk_max_chars"
            )


# call(text, purpose, chunk_id, max_output_chars) -> summary text
SummarizeCall = Callable[[str, ProviderCallPurpose, str | None, int], str]


@dataclass(frozen=True)
class DocumentSummary:
    final: str
    sections: list[str]  # one summary per chunk, in order
    combine_rounds: int


def summarize_document(
    chunk_ids_and_texts: list[tuple[str, str]],
    call: SummarizeCall,
    settings: SummarySettings,
) -> DocumentSummary:
    sections = [
        call(text, ProviderCallPurpose.CHUNK, chunk_id, settings.chunk_max_chars)
        for chunk_id, text in chunk_ids_and_texts
    ]
    if len(sections) == 1:
        return DocumentSummary(final=sections[0], sections=sections, combine_rounds=0)
    final, rounds = reduce_summaries(sections, call, settings)
    return DocumentSummary(final=final, sections=sections, combine_rounds=rounds)


def reduce_summaries(
    summaries: list[str], call: SummarizeCall, settings: SummarySettings
) -> tuple[str, int]:
    """Combine two or more summaries (each at most chunk_max_chars or final_max_chars) into one.
    Returns (final summary, number of combine rounds)."""
    level, rounds = summaries, 0
    while True:
        rounds += 1
        outputs = [
            call(batch, ProviderCallPurpose.COMBINE, None, settings.final_max_chars)
            for batch in _batches(level, settings.combine_max_input_chars)
        ]
        if len(outputs) == 1:
            return outputs[0], rounds
        if len(outputs) >= len(level):  # a round must shrink the list, or this never ends
            raise RuntimeError("combine round did not reduce the number of summaries")
        level = outputs


def _batches(items: list[str], max_chars: int) -> list[str]:
    """Pack items (one per line) into batches of at most max_chars."""
    batches: list[str] = []
    current: list[str] = []
    size = 0
    for item in items:
        line = " ".join(item.split())  # one summary per line
        added = len(line) + (1 if current else 0)
        if current and size + added > max_chars:
            batches.append("\n".join(current))
            current, size = [], 0
            added = len(line)
        current.append(line)
        size += added
    if current:
        batches.append("\n".join(current))
    return batches


def estimated_calls(chunk_count: int, settings: SummarySettings) -> int:
    """Rough number of provider calls, for progress reporting only."""
    if chunk_count <= 1:
        return chunk_count
    per_batch = max(2, settings.combine_max_input_chars // (settings.chunk_max_chars + 1))
    combines, items = 0, chunk_count
    while items > 1:
        items = -(-items // per_batch)
        combines += items
    return chunk_count + combines


def reduce_calls_bound(items: int, item_max_chars: int, settings: SummarySettings) -> int:
    """An upper bound on the combine calls reduce_summaries makes for `items` summaries of at most
    `item_max_chars` each. Greedy packing puts at least combine_max // (item_max + 1) items in
    every batch but the last, and later rounds combine summaries of at most final_max_chars."""
    calls = 0
    while items > 1:
        per_batch = max(2, settings.combine_max_input_chars // (item_max_chars + 1))
        items = -(-items // per_batch)
        calls += items
        item_max_chars = settings.final_max_chars
    return calls
