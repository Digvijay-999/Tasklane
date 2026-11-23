"""Build the repo_index artifacts' content: summary.md and manifest.json (pure functions).

Both are deterministic (no timestamps or timings), so rerunning a job with the same input and
settings produces byte-identical files.

Untrusted text, and how it is made inert in summary.md:
- File paths come from inside the archive. They are shown with Markdown punctuation escaped and
  invisible/format characters (e.g. right-to-left overrides that disguise a name) written as
  visible \\u escapes.
- Summaries are derived from repository content (with an LLM provider, they could contain
  anything). Each is put in a fenced block whose fence is longer than any run of fence
  characters inside it, so it always renders as plain preformatted text: no links, no HTML.
manifest.json is written as pure ASCII JSON (non-ASCII characters as \\u escapes), so it is
valid JSON and can't carry invisible characters to a viewer.
"""

from typing import Any

from app.domain.costs import Pricing
from app.workflows.pdf.report import (
    LLM_NOTICE,
    STUB_NOTICE,
    escape_markdown,
    fenced,
    usage_section,
    visible,
)

MANIFEST_SCHEMA = "tasklane.repo_manifest/1"


def display_path(path: str) -> str:
    """A path safe to show in Markdown: invisible characters made visible, punctuation
    escaped."""
    return escape_markdown(visible(path))


def _lines_label(file: dict[str, Any]) -> str:
    return "1 line" if file["lines"] == 1 else f"{file['lines']} lines"


def build_summary_markdown(
    *,
    display_filename: str,
    filtered: dict[str, Any],
    summary: dict[str, Any],
    pricing: Pricing | None = None,
    chunk_count: int,
) -> str:
    totals = filtered["totals"]
    lines = [
        f"# Repository summary of {escape_markdown(display_filename)}",
        "",
        f"{totals['included']} files indexed, {totals['skipped']} skipped; "
        f"{totals['included_lines']} lines in {chunk_count} chunks. "
        f"Summarized by `{summary['provider']}` / `{summary['model']}`.",
    ]
    lines += ["", STUB_NOTICE if summary["provider"] == "stub" else LLM_NOTICE]
    lines += ["", "## Overview", "", fenced(summary["overview"]), "", "## Files", ""]
    files = {f["path"]: f for f in filtered["included"]}
    for item in summary["files"]:
        file = files[item["path"]]
        lines += [
            f"### {display_path(item['path'])}",
            "",
            f"{file['language']}, {_lines_label(file)}, {item['chunks']} "
            f"{'chunk' if item['chunks'] == 1 else 'chunks'}.",
            "",
            fenced(item["summary"]),
            "",
        ]
    if filtered["skip_reasons"]:
        lines += ["## Skipped files", "", "| Reason | Files |", "| --- | ---: |"]
        lines += [f"| {reason} | {n} |" for reason, n in filtered["skip_reasons"].items()]
        lines += ["", "The manifest lists every skipped file with its reason."]
    return "\n".join(lines).rstrip("\n") + "\n"


def build_manifest(
    *,
    job_id: str,
    workflow_type: str,
    upload: dict[str, Any],
    archive: dict[str, Any],
    filtered: dict[str, Any],
    chunking: dict[str, Any],
    summary: dict[str, Any],
    pricing: Pricing | None = None,
    limits: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    entries = archive["entries"]
    return {
        "schema": MANIFEST_SCHEMA,
        "job_id": job_id,
        "workflow": workflow_type,
        "input": {
            "upload_id": upload["upload_id"],
            "filename": upload["filename"],
            "sha256": upload["sha256"],
            "size_bytes": upload["size_bytes"],
            "archive_entries": len(entries),
            "folders": sum(1 for e in entries if e["kind"] == "dir"),
        },
        "totals": filtered["totals"],
        "skip_reasons": filtered["skip_reasons"],
        "languages": filtered["languages"],
        "included": filtered["included"],
        "skipped": filtered["skipped"],
        "chunking": chunking,
        "summary": {
            "provider": summary["provider"],
            "model": summary["model"],
            "file_summaries": len(summary["files"]),
            "overview_combine_rounds": summary["combine_rounds"],
        },
        "usage": usage_section(summary, pricing),
        # Secrets replaced before text was sent to a real provider: counts only, never values.
        # Null with the stub (nothing leaves the machine).
        "secret_scrubbing": summary.get("secret_scrubbing"),
        "limits": limits,
        "artifacts": artifacts,
    }
