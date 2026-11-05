"""Pure upload rules: extensions, declared types, signatures, filenames."""

import pytest

from app.domain.enums import UploadKind, WorkflowType
from app.domain.uploads import (
    SIGNATURE_LENGTH,
    declared_type_matches,
    filename_problem,
    kind_from_filename,
    required_upload_kind,
    signature_matches,
)
from tests.samples import pdf_bytes, zip_bytes

PDF, ZIP = UploadKind.PDF, UploadKind.ZIP


@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("report.pdf", PDF),
        ("REPORT.PDF", PDF),
        ("archive.v2.zip", ZIP),
        ("../../etc/evil.pdf", PDF),  # display-only name; never used as a path
        ("C:\\Users\\me\\repo.zip", ZIP),
        ("notes.txt", None),
        ("pdf", None),
        ("report.pdf.exe", None),
        ("noextension", None),
    ],
)
def test_kind_from_filename(filename, kind):
    assert kind_from_filename(filename) == kind


@pytest.mark.parametrize(
    ("filename", "has_problem"),
    [
        ("report.pdf", False),
        ("résumé 2026.pdf", False),
        ("", True),
        ("   ", True),
        ("a" * 252 + ".pdf", True),  # 256 characters
        ("a" * 251 + ".pdf", False),  # 255 characters
        ("bad\nname.pdf", True),
        ("bad\x00name.pdf", True),
        ("invoice‮gpj.pdf", True),  # right-to-left override: displays as "invoicefdp.jpg"
        ("name⁦x⁩.pdf", True),  # bidi isolate
        ("line break.pdf", True),  # line separator
        ("emoji \U0001f600 name.pdf", False),
        ("café.pdf", False),
    ],
)
def test_filename_problem(filename, has_problem):
    assert (filename_problem(filename) is not None) == has_problem


@pytest.mark.parametrize(
    ("kind", "header", "ok"),
    [
        (PDF, "application/pdf", True),
        (PDF, "Application/PDF; charset=binary", True),
        (PDF, "application/zip", False),
        (PDF, "application/octet-stream", False),
        (PDF, "application/x-www-form-urlencoded", False),  # curl's default for --data-binary
        (PDF, None, False),
        (ZIP, "application/zip", True),
        (ZIP, "application/x-zip-compressed", True),  # sent by Windows browsers
        (ZIP, "application/octet-stream", True),
        (ZIP, "application/pdf", False),
        (ZIP, "", False),
    ],
)
def test_declared_type_matches(kind, header, ok):
    assert declared_type_matches(kind, header) == ok


@pytest.mark.parametrize(
    ("kind", "head", "ok"),
    [
        (PDF, pdf_bytes()[:SIGNATURE_LENGTH], True),
        (PDF, b"%PDF", False),  # too short to be a PDF
        (PDF, zip_bytes()[:SIGNATURE_LENGTH], False),
        (PDF, b"\xef\xbb\xbf%PDF-", False),  # leading bytes before the signature
        (ZIP, zip_bytes()[:SIGNATURE_LENGTH], True),
        (ZIP, b"PK\x05\x06\x00", False),  # empty archive
        (ZIP, b"PK\x07\x08\x00", False),  # spanned archive
        (ZIP, pdf_bytes()[:SIGNATURE_LENGTH], False),
        (ZIP, b"", False),
    ],
)
def test_signature_matches(kind, head, ok):
    assert signature_matches(kind, head) == ok


def test_workflows_require_matching_upload_kinds():
    assert required_upload_kind(WorkflowType.PDF_INGEST) == PDF
    assert required_upload_kind(WorkflowType.REPO_INDEX) == ZIP
