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

import re
import unicodedata
from typing import Any

from app.workflows.pdf.report import STUB_NOTICE, escape_markdown

MANIFEST_SCHEMA = "tasklane.repo_manifest/1"


def visible(text: str, keep: str = "") -> str:
    """Control and format characters (category C*, e.g. right-to-left overrides, zero-width
    spaces) written as visible \\uXXXX text, except those in `keep`."""
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char)[0] == "C" and char not in keep else char
        for char in text
    )


def display_path(path: str) -> str:
    """A path safe to show in Markdown: invisible characters made visible, punctuation
    escaped."""
    return escape_markdown(visible(path))


def fenced(text: str) -> str:
    """Put text in a fenced code block it can't break out of, with invisible characters (other
    than line breaks and tabs) made visible."""
    text = visible(text, keep="\n\t")
    longest = max((len(run) for run in re.findall(r"~+", text)), default=0)
    fence = "~" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _lines_label(file: dict[str, Any]) -> str:
    return "1 line" if file["lines"] == 1 else f"{file['lines']} lines"


def build_summary_markdown(
    *,
    display_filename: str,
    filtered: dict[str, Any],
    summary: dict[str, Any],
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
    if summary["provider"] == "stub":
        lines += ["", STUB_NOTICE]
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
        "usage": {
            "provider_calls": summary["calls"],
            # Token counts only when the provider reports them; never estimated.
            "total_tokens": summary.get("total_tokens"),
        },
        "limits": limits,
        "artifacts": artifacts,
    }
