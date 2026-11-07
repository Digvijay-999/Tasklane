"""Split source files into chunks of whole lines (pure and deterministic).

Each file is chunked on its own (a chunk never spans two files). Lines are packed greedily with
the same size settings and the same packing code as PDFs (app/workflows/pdf/chunking.py):
about `target_chars` per chunk, never more than `max_chars`, and each chunk after a file's first
starts with that file's previous lines up to `overlap_chars`. A single line longer than
`max_chars` (rare in source code) is cut into pieces that keep its line number.

Chunk IDs use the PDF scheme with this chunker's own version:
    c{index:04d}-{first 12 hex of sha256(input_sha256|version|sizes|index|chunk text)}
where `index` counts chunks across the whole repository.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from app.workflows.pdf.chunking import ChunkingSettings, Piece, chunk_id, group_pieces

REPO_CHUNKER_VERSION = "repo-lines-1"


@dataclass(frozen=True)
class SourceChunk:
    chunk_id: str
    index: int
    path: str
    line_start: int
    line_end: int
    text: str
    sha256: str

    def to_record(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "index": self.index,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "sha256": self.sha256,
            "chars": len(self.text),
            "text": self.text,
        }


def source_lines(text: str) -> list[str]:
    """The file's lines (a final line break doesn't start another, empty line)."""
    return (text[:-1] if text.endswith("\n") else text).split("\n")


def chunk_files(
    files: Iterable[tuple[str, str]], *, input_sha256: str, settings: ChunkingSettings
) -> list[SourceChunk]:
    """files: (path, text) in a fixed order. Returns every chunk of every file, in that order."""
    chunks: list[SourceChunk] = []
    for path, text in files:
        pieces = [
            Piece(line[start : start + settings.max_chars], number)
            for number, line in enumerate(source_lines(text), start=1)
            for start in range(0, max(len(line), 1), settings.max_chars)
        ]
        for group in group_pieces(pieces, settings):
            index = len(chunks)
            body = "\n".join(piece.text for piece in group)
            chunks.append(
                SourceChunk(
                    chunk_id=chunk_id(input_sha256, REPO_CHUNKER_VERSION, settings, index, body),
                    index=index,
                    path=path,
                    line_start=group[0].page,
                    line_end=group[-1].page,
                    text=body,
                    sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                )
            )
    return chunks
