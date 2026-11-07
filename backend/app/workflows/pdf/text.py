"""Text normalization and the "too little text" rule (pure functions)."""

import re
import unicodedata

# A document with less extractable text than this is treated as scanned/image-only.
MIN_AVERAGE_VISIBLE_CHARS_PER_PAGE = 20
MIN_TOTAL_VISIBLE_CHARS = 100
# A page with fewer visible characters counts as "without text" in the manifest.
PAGE_WITH_TEXT_MIN_CHARS = 20

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """NFC normalization, control characters removed, runs of spaces collapsed, at most one
    blank line in a row, no trailing spaces on lines."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def visible_chars(text: str) -> int:
    """Characters that aren't whitespace."""
    return sum(1 for char in text if not char.isspace())


def has_enough_text(total_visible_chars: int, page_count: int) -> bool:
    """The scanned-PDF rule: at least 20 visible characters per page on average AND at least 100
    in total. A fully scanned document has (almost) none and fails; a document that is half text
    and half scans passes, and the manifest reports its pages without text."""
    if page_count <= 0:
        return False
    return (
        total_visible_chars >= MIN_TOTAL_VISIBLE_CHARS
        and total_visible_chars / page_count >= MIN_AVERAGE_VISIBLE_CHARS_PER_PAGE
    )
