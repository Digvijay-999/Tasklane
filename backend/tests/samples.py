"""Small sample files for upload tests."""

import io
import zipfile


def pdf_bytes(size: int | None = None) -> bytes:
    """A file starting with the PDF signature, padded to `size` bytes if given."""
    content = b"%PDF-1.4\n1 0 obj << >> endobj\n%%EOF\n"
    if size is not None:
        content = content[:size].ljust(size, b"%")
    return content


def zip_bytes(size: int | None = None) -> bytes:
    """A real ZIP archive. If `size` is given, zero bytes are appended to reach it.

    The padding sits after the end-of-central-directory record without being declared, so a
    padded file still *starts* like a ZIP (all Phase 3 checks) but is not a well-formed archive.
    Don't reuse padded samples for Phase 6 content checks.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.md", "# sample repository\n")
        archive.writestr("src/main.py", "print('hello')\n")
    content = buffer.getvalue()
    if size is not None and size > len(content):
        content += b"\0" * (size - len(content))
    return content
