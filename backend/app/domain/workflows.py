"""The stages each workflow runs, in order, with their share of the progress bar.

Clients (the dashboard's timeline) read this through GET /v1/meta, so they never hardcode stage
lists. The worker builds its real stage objects in app/workflows/registry.py; a unit test checks
that both lists agree, name for name and weight for weight.
"""

from app.domain.enums import WorkflowType

WORKFLOW_STAGES: dict[WorkflowType, tuple[tuple[str, int], ...]] = {
    WorkflowType.PDF_INGEST: (
        ("validate_pdf", 5),
        ("extract_text", 30),
        ("chunk_document", 5),
        ("summarize", 55),
        ("write_artifacts", 5),
    ),
    WorkflowType.REPO_INDEX: (
        ("validate_archive", 5),
        ("extract_files", 15),
        ("filter_files", 5),
        ("chunk_sources", 5),
        ("summarize", 65),
        ("write_artifacts", 5),
    ),
}
