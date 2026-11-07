"""Split a document's pages into overlapping chunks (pure and deterministic).

Method:
1. Split each page into paragraphs (blank lines), and paragraphs into sentences. Each piece
   remembers its page number.
2. Pack pieces greedily into chunks of about `target_chars`, never more than `max_chars`. A
   single piece longer than `max_chars` is split at word boundaries.
3. Overlap: each chunk after the first starts with the last whole sentences of the previous
   chunk, up to `overlap_chars`, so a thought cut at a boundary appears in both chunks.

Sizes are in characters (4,000 characters is roughly 1,000 tokens), which avoids depending on a
model-specific tokenizer.

Chunk IDs are deterministic: the same file with the same settings always gives the same IDs:
    c{index:04d}-{first 12 hex of sha256(input_sha256|version|sizes|index|chunk text)}
Changing the settings or the chunker version changes the IDs (and so invalidates anything cached
per chunk in Part B).
"""

import hashlib
import re
from dataclasses import dataclass

CHUNKER_VERSION = "1"

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkingSettings:
    target_chars: int = 4000
    max_chars: int = 6000
    overlap_chars: int = 400

    def __post_init__(self) -> None:
        if not (0 <= self.overlap_chars < self.target_chars <= self.max_chars):
            raise ValueError("chunking sizes must satisfy 0 <= overlap < target <= max")


@dataclass(frozen=True)
class Piece:
    text: str
    page: int  # page number (PDF) or line number (repo source file)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    index: int
    text: str
    sha256: str
    page_start: int
    page_end: int

    def to_record(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "index": self.index,
            "text": self.text,
            "sha256": self.sha256,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "chars": len(self.text),
        }


def split_pieces(pages: list[tuple[int, str]], max_chars: int) -> list[Piece]:
    """Pages -> sentence-sized pieces (each at most max_chars), in reading order."""
    pieces: list[Piece] = []
    for page, text in pages:
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = " ".join(paragraph.split())
            for sentence in _SENTENCE_END.split(paragraph):
                if not sentence:
                    continue
                for part in _hard_split(sentence, max_chars):
                    pieces.append(Piece(part, page))
    return pieces


def chunk_pages(
    pages: list[tuple[int, str]], *, input_sha256: str, settings: ChunkingSettings
) -> list[Chunk]:
    pieces = split_pieces(pages, settings.max_chars)
    chunks = []
    for index, group in enumerate(group_pieces(pieces, settings)):
        text = " ".join(p.text for p in group)
        chunks.append(
            Chunk(
                chunk_id=chunk_id(input_sha256, CHUNKER_VERSION, settings, index, text),
                index=index,
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                page_start=group[0].page,
                page_end=group[-1].page,
            )
        )
    return chunks


def group_pieces(pieces: list[Piece], settings: ChunkingSettings) -> list[list[Piece]]:
    """Pack pieces greedily into groups of about target_chars (never more than max_chars, counting
    one separator character between pieces), each group after the first starting with the
    previous group's last whole pieces up to overlap_chars. Also used by the repo chunker, where
    a piece is one line."""
    groups: list[list[Piece]] = []
    current: list[Piece] = []
    for piece in pieces:
        if current and _length(current + [piece]) > settings.target_chars:
            groups.append(current)
            current = _overlap_tail(current, settings.overlap_chars)
            # The overlap must leave room for new text, or the chunk could never grow.
            while current and _length(current + [piece]) > settings.max_chars:
                current = current[1:]
        current.append(piece)
    if current and (not groups or current != groups[-1]):
        groups.append(current)
    return groups


def chunk_id(
    input_sha256: str, version: str, settings: ChunkingSettings, index: int, text: str
) -> str:
    """c{index:04d}-{first 12 hex of sha256(input_sha256|version|sizes|index|text)}."""
    identity = "|".join(
        [
            input_sha256,
            version,
            f"{settings.target_chars}/{settings.max_chars}/{settings.overlap_chars}",
            str(index),
            text,
        ]
    )
    return f"c{index:04d}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}"


def _length(pieces: list[Piece]) -> int:
    return sum(len(p.text) for p in pieces) + max(0, len(pieces) - 1)


def _overlap_tail(pieces: list[Piece], overlap_chars: int) -> list[Piece]:
    """The longest run of whole trailing pieces whose length fits in overlap_chars."""
    tail: list[Piece] = []
    for piece in reversed(pieces):
        if _length([piece] + tail) > overlap_chars:
            break
        tail.insert(0, piece)
    return tail


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Split text longer than max_chars at word boundaries (or mid-word if a word is huge)."""
    if len(text) <= max_chars:
        return [text]
    parts, current = [], ""
    for word in text.split(" "):
        while len(word) > max_chars:  # a single enormous "word"
            if current:
                parts.append(current)
                current = ""
            parts.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > max_chars:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
