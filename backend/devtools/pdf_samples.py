"""Generate small sample PDFs in code (no binary files in the repository).

Used by the tests, and by the manual walkthrough:

    python -m devtools.pdf_samples <output folder>

writes report.pdf (text, several pages), scanned.pdf (images only, no text), encrypted.pdf
(needs a password), owner_locked.pdf (has only an owner password, opens without one), and
corrupt.pdf (a truncated file), and large.pdf (500 pages, a few seconds of parsing).

The text PDFs use Helvetica, one of the 14 standard fonts every PDF reader has built in, so no
font data needs embedding and the files stay tiny and byte-for-byte deterministic.
"""

import io
import sys
import zlib
from pathlib import Path

_TOPICS = [
    "durable job state",
    "transactional outboxes",
    "idempotent workers",
    "lease based recovery",
    "bounded retries with jitter",
    "event replay for clients",
    "safe handling of untrusted files",
    "observability for background work",
]


def sample_paragraphs(count: int, seed: int = 0) -> list[str]:
    """Deterministic English-like paragraphs (no randomness: same input, same text)."""
    paragraphs = []
    for i in range(count):
        topic = _TOPICS[(i + seed) % len(_TOPICS)]
        other = _TOPICS[(i + seed + 3) % len(_TOPICS)]
        paragraphs.append(
            f"Section {i + 1} discusses {topic}. A reliable system records every important "
            f"change in a database before acting on it. This section also compares {topic} "
            f"with {other} and explains why both matter when jobs run for a long time. "
            f"Operators can inspect the history of each job and understand what happened."
        )
    return paragraphs


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int = 90) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _assemble(objects: list[bytes]) -> bytes:
    """Write objects 1..N, a cross-reference table, and a trailer (root is object 1)."""
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _stream(content: bytes, extra: str = "") -> bytes:
    return f"<< /Length {len(content)} {extra}>>\nstream\n".encode() + content + b"\nendstream"


def text_pdf(pages: list[list[str]]) -> bytes:
    """A PDF whose page i shows the paragraphs pages[i] as text."""
    # Objects: 1 catalog, 2 page tree, 3 font, then (page, content) pairs.
    page_numbers = [4 + 2 * i for i in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_numbers)}] "
        f"/Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, paragraphs in enumerate(pages):
        lines = []
        for paragraph in paragraphs:
            lines.extend(_wrap(paragraph))
            lines.append("")
        ops = ["BT", "/F1 11 Tf", "14 TL", "60 750 Td"]
        for line in lines[:50]:  # one page holds about 50 lines
            ops.append(f"({_escape(line)}) Tj T*")
        ops.append("ET")
        content = "\n".join(ops).encode("latin-1")
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {page_numbers[index] + 1} 0 R >>"
            ).encode()
        )
        objects.append(_stream(content))
    return _assemble(objects)


def report_pdf(page_count: int = 3, paragraphs_per_page: int = 3) -> bytes:
    """A normal multi-page text document."""
    texts = sample_paragraphs(page_count * paragraphs_per_page)
    return text_pdf(
        [texts[i * paragraphs_per_page : (i + 1) * paragraphs_per_page] for i in range(page_count)]
    )


def image_only_pdf(page_count: int = 2) -> bytes:
    """Pages that show only an image and contain no text (like a scan without OCR)."""
    size = 64
    pixels = bytes(((x * 4) ^ (y * 4)) & 0xFF for y in range(size) for x in range(size))
    image = zlib.compress(pixels)
    page_numbers = [4 + 2 * i for i in range(page_count)]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_numbers)}] "
        f"/Count {page_count} >>".encode(),
        _stream(
            image,
            f"/Type /XObject /Subtype /Image /Width {size} /Height {size} "
            "/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode ",
        ),
    ]
    for index in range(page_count):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /XObject << /Im1 3 0 R >> >> "
            f"/Contents {page_numbers[index] + 1} 0 R >>".encode()
        )
        objects.append(_stream(b"q 400 0 0 400 100 200 cm /Im1 Do Q"))
    return _assemble(objects)


def encrypted_pdf(user_password: str = "secret", owner_password: str = "owner") -> bytes:
    """A text PDF encrypted with AES-256. With user_password="" it opens without a password
    (only an owner password restricts printing/copying)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(report_pdf(page_count=2))))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="AES-256")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def corrupt_pdf() -> bytes:
    """A file that starts like a PDF but is cut off before any page exists."""
    return report_pdf()[:300]


SAMPLES = {
    "report.pdf": lambda: report_pdf(page_count=4),
    "scanned.pdf": lambda: image_only_pdf(page_count=3),
    "encrypted.pdf": lambda: encrypted_pdf(),
    "owner_locked.pdf": lambda: encrypted_pdf(user_password=""),
    "corrupt.pdf": corrupt_pdf,
    # 500 pages (the page limit): its text extraction takes a few seconds, long enough to stop a
    # worker in the middle of parsing (Phase 7 walkthrough).
    "large.pdf": lambda: report_pdf(page_count=500, paragraphs_per_page=12),
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m devtools.pdf_samples <output folder>", file=sys.stderr)
        return 2
    folder = Path(args[0])
    folder.mkdir(parents=True, exist_ok=True)
    for name, build in SAMPLES.items():
        (folder / name).write_bytes(build())
        print(f"wrote {folder / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
