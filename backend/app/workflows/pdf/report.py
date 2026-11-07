"""Build the final artifacts: summary.md and manifest.json (pure functions).

Both are deterministic: no timestamps or timings in the content, so rerunning a job with the same
input and settings produces byte-identical files. (Creation times and per-call latencies are in
the database.)
"""

import re
from typing import Any

MANIFEST_SCHEMA = "tasklane.pdf_manifest/1"

# Characters that can change how a line renders: emphasis, code, links, HTML, headings, tables.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~])")


def escape_markdown(text: str) -> str:
    """Escape a user-supplied string (the display filename) so it renders as plain text."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def build_summary_markdown(
    *, display_filename: str, extract: dict[str, Any], summary: dict[str, Any]
) -> str:
    lines = [
        f"# Summary of {escape_markdown(display_filename)}",
        "",
        f"{extract['pages']} pages, {extract['characters']} characters of text. "
        f"Summarized by `{summary['provider']}` / `{summary['model']}`.",
    ]
    if summary["provider"] == "stub":
        lines += [
            "",
            "> This summary was produced by the built-in stub provider, which extracts leading "
            "sentences and frequent terms. It is a placeholder, not an AI-generated summary.",
        ]
    if extract.get("pages_without_text"):
        lines += [
            "",
            f"> {extract['pages_without_text']} of {extract['pages']} pages had no extractable "
            "text (for example scanned images) and are not covered.",
        ]
    lines += ["", "## Summary", "", summary["final"], "", "## Sections", ""]
    for section in summary["sections"]:
        pages = (
            f"page {section['page_start']}"
            if section["page_start"] == section["page_end"]
            else f"pages {section['page_start']}-{section['page_end']}"
        )
        lines.append(f"- **{pages}:** {section['summary']}")
    return "\n".join(lines) + "\n"


def build_manifest(
    *,
    job_id: str,
    workflow_type: str,
    upload: dict[str, Any],
    validate: dict[str, Any],
    extract: dict[str, Any],
    chunking: dict[str, Any],
    summary: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "job_id": job_id,
        "workflow": workflow_type,
        "input": {
            "upload_id": upload["upload_id"],
            "filename": upload["filename"],
            "sha256": upload["sha256"],
            "size_bytes": upload["size_bytes"],
            "pages": extract["pages"],
            "pages_with_text": extract["pages_with_text"],
            "pages_without_text": extract["pages_without_text"],
            "characters": extract["characters"],
            "owner_password_only": validate["owner_password_only"],
        },
        "chunking": chunking,
        "summary": {
            "provider": summary["provider"],
            "model": summary["model"],
            "chunk_summaries": len(summary["sections"]),
            "combine_rounds": summary["combine_rounds"],
        },
        "usage": {
            "provider_calls": summary["calls"],
            # Token counts only when the provider reports them; never estimated.
            "total_tokens": summary.get("total_tokens"),
        },
        "artifacts": artifacts,
    }
