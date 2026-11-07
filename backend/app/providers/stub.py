"""A deterministic, offline stand-in for an AI summarizer.

It never touches the network and needs no API key: it *extracts* leading sentences and the most
frequent longer words. The same input always gives the same output, which makes tests and reruns
reproducible. Its output is labelled as a placeholder in the summary artifact; it is not an AI
summary.
"""

import re
import time
from collections import Counter

from app.domain.enums import ProviderCallPurpose
from app.providers.base import SummarizeOptions, SummaryResult

STUB_PROVIDER = "stub"
STUB_MODEL = "stub-extractive-v1"

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z][a-z'-]{3,}")
_STOP_WORDS = frozenset(
    "about after also because before being between both could does each from have into "
    "just more most much only other over same should some such than that their them then "
    "there these they this those through very what when where which while will with would "
    "your section".split()
)
_KEY_TERMS = 5


class StubProvider:
    name = STUB_PROVIDER
    model = STUB_MODEL

    def __init__(self, delay_seconds: float = 0) -> None:
        # Simulated latency (development and tests only: STUB_DELAY_SECONDS), so a job runs long
        # enough to watch its progress or to stop a worker in the middle of it.
        self.delay_seconds = delay_seconds

    def summarize(self, text: str, options: SummarizeOptions) -> SummaryResult:
        started = time.perf_counter()
        if self.delay_seconds:
            time.sleep(min(self.delay_seconds, options.timeout_seconds))
        if options.purpose == ProviderCallPurpose.COMBINE:
            # Combining: the input is one summary per line; keep the lead sentence of each.
            sentences = [_first_sentence(line) for line in text.splitlines() if line.strip()]
            limit = None
        else:
            sentences = _sentences(text)
            limit = 2
        summary = _join_within(sentences, options.max_output_chars, max_sentences=limit)
        terms = _key_terms(text)
        if terms:
            suffix = f" Key terms: {', '.join(terms)}."
            if len(summary) + len(suffix) <= options.max_output_chars:
                summary += suffix
        summary = summary[: options.max_output_chars]
        return SummaryResult(
            text=summary,
            provider=self.name,
            model=self.model,
            latency_ms=round((time.perf_counter() - started) * 1000),
            usage=None,  # the stub has no tokens to report
        )


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(" ".join(text.split())) if s.strip()]


def _first_sentence(text: str) -> str:
    sentences = _sentences(text)
    return sentences[0] if sentences else ""


def _join_within(sentences: list[str], max_chars: int, *, max_sentences: int | None) -> str:
    picked: list[str] = []
    for sentence in sentences:
        candidate = " ".join([*picked, sentence])
        if picked and len(candidate) > max_chars:
            break
        picked.append(sentence)
        if max_sentences is not None and len(picked) >= max_sentences:
            break
    return " ".join(picked)[:max_chars]


def _key_terms(text: str) -> list[str]:
    counts = Counter(w for w in _WORD.findall(text.lower()) if w not in _STOP_WORDS)
    # Most frequent first; ties broken alphabetically, so the result is deterministic.
    return [word for word, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_KEY_TERMS]]
