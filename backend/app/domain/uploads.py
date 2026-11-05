"""Rules for accepting uploaded files (pure functions, no I/O).

What is checked here (Phase 3): the file extension, the declared Content-Type, and the file
signature ("magic bytes" at the start of the file). Deeper checks come later:
- ZIP (Phase 6): reading the archive's table of contents, path traversal, symlinks,
  decompression bombs, file-count limits, nested archives. A ZIP accepted now is only known to
  *start* like a ZIP; nothing opens or extracts it yet.
- PDF (Phase 5): parsing the document, encryption, scanned PDFs without extractable text.
"""

import unicodedata
from pathlib import PurePosixPath

from app.domain.enums import UploadKind, WorkflowType

KIND_BY_EXTENSION = {".pdf": UploadKind.PDF, ".zip": UploadKind.ZIP}

# The content type we store: the one proven by the file signature, never the client's claim.
DETECTED_CONTENT_TYPE = {UploadKind.PDF: "application/pdf", UploadKind.ZIP: "application/zip"}

# Content-Type values a client may declare for each kind. Windows browsers send
# application/x-zip-compressed for ZIPs; some clients send octet-stream when unsure.
ACCEPTED_DECLARED_TYPES = {
    UploadKind.PDF: frozenset({"application/pdf"}),
    UploadKind.ZIP: frozenset(
        {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
    ),
}

# Signatures. ZIP must begin with a *local file header*; an empty archive (PK\x05\x06) or a
# spanned/split one (PK\x07\x08) is rejected because there is nothing usable in it.
SIGNATURES = {UploadKind.PDF: b"%PDF-", UploadKind.ZIP: b"PK\x03\x04"}

# How many leading bytes are needed to check any signature.
SIGNATURE_LENGTH = max(len(signature) for signature in SIGNATURES.values())

# Which upload kind each workflow needs.
WORKFLOW_UPLOAD_KIND = {
    WorkflowType.PDF_INGEST: UploadKind.PDF,
    WorkflowType.REPO_INDEX: UploadKind.ZIP,
}

MAX_FILENAME_LENGTH = 255

# Invisible characters that reorder how text is displayed. "invoice‮gpj.pdf" displays as
# "invoicefdp.jpg", hiding the real extension from whoever reads the name in the dashboard.
_BIDI_CONTROLS = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
# Unicode categories rejected outright: control characters, and line/paragraph separators.
_REJECTED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def filename_problem(filename: str) -> str | None:
    """Return why a display filename is unacceptable, or None if it's fine.

    The filename is stored for display only and is never used to build a path, so a name like
    `../../x.pdf` is harmless; we only reject names that would be confusing or unprintable.
    """
    if not filename.strip():
        return "The filename is empty."
    if len(filename) > MAX_FILENAME_LENGTH:
        return f"The filename is longer than {MAX_FILENAME_LENGTH} characters."
    if any(unicodedata.category(char) in _REJECTED_CATEGORIES for char in filename):
        return "The filename contains control characters or line breaks."
    if any(char in _BIDI_CONTROLS for char in filename):
        return "The filename contains invisible text-direction characters."
    return None


def kind_from_filename(filename: str) -> UploadKind | None:
    """The upload kind implied by the extension (case-insensitive), or None if unsupported."""
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return KIND_BY_EXTENSION.get(suffix)


def declared_type_matches(kind: UploadKind, content_type_header: str | None) -> bool:
    """Whether the client's Content-Type header is acceptable for this kind.

    Parameters such as `; charset=...` are ignored. A missing header is not acceptable: curl and
    browsers would otherwise send form-encoding defaults that say nothing about the file.
    """
    if not content_type_header:
        return False
    media_type = content_type_header.split(";", 1)[0].strip().lower()
    return media_type in ACCEPTED_DECLARED_TYPES[kind]


def signature_matches(kind: UploadKind, head: bytes) -> bool:
    """Whether the first bytes of the file carry the signature for this kind."""
    return head.startswith(SIGNATURES[kind])


def required_upload_kind(workflow_type: WorkflowType) -> UploadKind:
    return WORKFLOW_UPLOAD_KIND[workflow_type]
