"""Every PDF stage is safe to run twice: same input -> byte-identical outputs, same artifacts.

Runs the real stages (real parser process, stub provider) against a real workspace on disk, with
a recording StageContext instead of the database.
"""

import hashlib
import json
import uuid

import pytest

from app.domain.enums import UploadKind, WorkflowType
from app.providers.stub import StubProvider
from app.queue.worker_process import workflow_config
from app.storage.local import LocalStorage
from app.storage.workspace import JobWorkspace
from app.workflows.base import JobInput, UploadInput
from app.workflows.registry import stages_for
from devtools import pdf_samples


class RecordingContext:
    def __init__(self, workspace: JobWorkspace, job: JobInput) -> None:
        self.workspace = workspace
        self.job = job
        self.progress: list[int] = []
        self.keep_alives = 0
        self.calls = []
        self.artifacts = []

    def report_progress(self, percent: int) -> None:
        self.progress.append(percent)

    def keep_alive(self) -> None:
        self.keep_alives += 1

    def record_provider_call(self, call) -> None:
        self.calls.append(call)

    def save_artifact(self, artifact) -> None:
        self.artifacts.append(artifact)


@pytest.fixture
def pipeline(tmp_path, unit_settings):
    storage = LocalStorage(tmp_path / "storage")
    upload_id, job_id = uuid.uuid4(), uuid.uuid4()
    pdf = pdf_samples.report_pdf(page_count=6)
    upload_path = storage.upload_path(upload_id, UploadKind.PDF)
    target = storage.absolute(upload_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(pdf)
    job = JobInput(
        job_id=job_id,
        attempt_no=1,
        workflow_type=WorkflowType.PDF_INGEST.value,
        upload=UploadInput(
            upload_id=upload_id,
            storage_path=upload_path,
            sha256=hashlib.sha256(pdf).hexdigest(),
            size_bytes=len(pdf),
            original_filename="report.pdf",
        ),
    )
    workspace = JobWorkspace(storage, job_id)
    stages = stages_for(WorkflowType.PDF_INGEST, workflow_config(unit_settings))
    return stages, workspace, job


def _snapshot(workspace: JobWorkspace) -> dict[str, str]:
    """Every file under work/ and artifacts/ for this job, with its SHA-256."""
    files = {}
    for area in ("work", "artifacts"):
        root = workspace.storage.root / area / str(workspace.job_id)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(workspace.storage.root))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return files


def test_the_pipeline_produces_the_expected_files(pipeline):
    stages, workspace, job = pipeline
    context = RecordingContext(workspace, job)
    for stage in stages:
        stage.run(context)

    files = _snapshot(workspace)
    names = {path.rsplit("/", 1)[-1] for path in files if "/summaries/" not in path}
    assert names == {
        "validate.json",
        "pages.jsonl",
        "extract.json",
        "chunks.jsonl",
        "summary.json",
        "summary.md",
        "manifest.json",
    }  # no leftover temporary files
    # One cached summary per chunk (what a retry reuses instead of calling the provider again).
    chunk_ids = {c["chunk_id"] for c in workspace.iter_jsonl(workspace.work("chunks.jsonl"))}
    cached = {p.rsplit("/", 1)[-1] for p in files if "/summaries/" in p}
    assert cached == {f"{chunk_id}.json" for chunk_id in chunk_ids}
    assert [a.artifact_type for a in context.artifacts] == ["summary", "manifest"]
    assert context.calls and all(c.success and c.provider == "stub" for c in context.calls)


@pytest.mark.parametrize(
    "stage_name",
    ["validate_pdf", "extract_text", "chunk_document", "summarize", "write_artifacts"],
)
def test_rerunning_a_stage_produces_identical_outputs(pipeline, stage_name):
    stages, workspace, job = pipeline
    first = RecordingContext(workspace, job)
    for stage in stages:
        stage.run(first)
    before = _snapshot(workspace)

    [stage] = [s for s in stages if s.name == stage_name]
    again = RecordingContext(workspace, job)
    stage.run(again)

    assert _snapshot(workspace) == before  # same files, byte for byte, nothing extra
    if stage_name == "write_artifacts":
        assert again.artifacts == first.artifacts  # same types, paths, sizes, hashes


def test_rerunning_the_whole_pipeline_is_identical(pipeline):
    stages, workspace, job = pipeline
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    before = _snapshot(workspace)
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    assert _snapshot(workspace) == before


# --- The per-chunk summary cache ------------------------------------------------------------------


def _summarize(stages):
    [stage] = [s for s in stages if s.name == "summarize"]
    return stage


def _chunk_calls(context) -> list:
    return [c for c in context.calls if c.purpose == "chunk"]


def test_a_rerun_reuses_every_chunk_summary(pipeline):
    stages, workspace, job = pipeline
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    again = RecordingContext(workspace, job)
    _summarize(stages).run(again)
    assert _chunk_calls(again) == []  # only the combine calls reach the provider


@pytest.mark.parametrize("change", ["corrupt file", "other length limit", "other stage version"])
def test_a_cached_summary_that_does_not_match_is_made_again(pipeline, change):
    stages, workspace, job = pipeline
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    summarize = _summarize(stages)
    chunk_ids = [c["chunk_id"] for c in workspace.iter_jsonl(workspace.work("chunks.jsonl"))]
    first = workspace.absolute(workspace.work(f"summaries/{chunk_ids[0]}.json"))
    if change == "corrupt file":
        first.write_text("{not json")
        expected = 1
    elif change == "other length limit":
        cached = json.loads(first.read_text())
        first.write_text(json.dumps({**cached, "max_output_chars": 1}))
        expected = 1
    else:
        summarize.stage_version += 1  # the stage's logic changed: nothing cached applies
        expected = len(chunk_ids)

    again = RecordingContext(workspace, job)
    summarize.run(again)

    assert len(_chunk_calls(again)) == expected


def test_another_model_does_not_reuse_cached_summaries(pipeline):
    stages, workspace, job = pipeline
    for stage in stages:
        stage.run(RecordingContext(workspace, job))
    summarize = _summarize(stages)
    renamed = StubProvider()
    renamed.model = "stub-extractive-v2"
    summarize.provider = renamed
    again = RecordingContext(workspace, job)
    summarize.run(again)
    chunks = sum(1 for _ in workspace.iter_jsonl(workspace.work("chunks.jsonl")))
    assert len(_chunk_calls(again)) == chunks
