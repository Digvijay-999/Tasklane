"""The PDF parser that runs in a separate, limited process. Started ONLY by sandbox.py as:

    python -I limited_launcher.py <limits...> parse_child.py
        <validate|extract> <input.pdf> <output file> <max_pages> <max_chars>

Why a separate process: the PDF comes from an untrusted upload. A crafted file can make a parser
loop forever or eat memory, and a stuck Python function can't be stopped from inside its own
process. Here, the parent kills this process at a deadline, and the operating system enforces
memory, CPU, and file-size limits on it (set by limited_launcher.py before this code runs).

Isolation: `python -I` means the application's own code isn't importable here, environment
variables like PYTHONPATH are ignored, and the parent passes no secrets in the environment. This
file may import only the standard library and pypdf (a test enforces that).

Protocol: one JSON object per line on stdout.
    {"type": "progress", "done": 3, "total": 10}
    {"type": "result", ...}                                  then exit 0
    {"type": "error", "error_class": "...", "params": {...}} then exit 0
Any other exit (a crash, or being killed) is interpreted by the parent.
"""

import errno
import json
import logging
import sys


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def fail(error_class: str, **params: int) -> None:
    emit({"type": "error", "error_class": error_class, "params": params})
    sys.exit(0)


def open_pdf(path: str, max_pages: int):  # type: ignore[no-untyped-def]
    from pypdf import PasswordType, PdfReader

    try:
        reader = PdfReader(path)
        owner_password_only = False
        if reader.is_encrypted:
            # PDFs restricted only by an *owner* password (no printing/copying) open with an empty
            # user password; viewers open them without asking, so we do too.
            if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                fail("pdf_encrypted")
            owner_password_only = True
        page_count = len(reader.pages)
    except MemoryError:
        raise
    except Exception:  # anything the parser can't make sense of
        fail("pdf_corrupt")
    if page_count == 0:
        fail("pdf_no_text")
    if page_count > max_pages:
        fail("pdf_too_many_pages", pages=page_count, limit=max_pages)
    return reader, page_count, owner_password_only


def validate(path: str, output: str, max_pages: int) -> None:
    reader, page_count, owner_password_only = open_pdf(path, max_pages)
    result = {
        "pages": page_count,
        "owner_password_only": owner_password_only,
        "pdf_header": str(getattr(reader, "pdf_header", ""))[:16],
    }
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    emit({"type": "result", **result})


def extract(path: str, output: str, max_pages: int, max_chars: int) -> None:
    reader, page_count, _ = open_pdf(path, max_pages)
    total = 0
    with open(output, "w", encoding="utf-8") as handle:
        for index, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except MemoryError:
                raise
            except Exception:
                fail("pdf_corrupt")
            total += len(text)
            if total > max_chars:  # stop as soon as the limit is passed; never truncate silently
                fail("pdf_text_too_large", limit=max_chars)
            handle.write(json.dumps({"page": index + 1, "text": text}) + "\n")
            emit({"type": "progress", "done": index + 1, "total": page_count})
    emit({"type": "result", "pages": page_count, "characters": total})


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.CRITICAL)  # pypdf warns about many harmless oddities
    mode, path, output, max_pages, max_chars = argv
    try:
        if mode == "validate":
            validate(path, output, int(max_pages))
        elif mode == "extract":
            extract(path, output, int(max_pages), int(max_chars))
        else:
            raise SystemExit(f"unknown mode {mode!r}")
    except OSError as exc:
        if exc.errno == errno.EFBIG:  # over the output-size limit set by the parent
            fail("pdf_too_complex")
        raise


if __name__ == "__main__":
    main(sys.argv[1:])
