"""Every PDF stage is safe to run twice: same input -> byte-identical outputs, same artifacts.

Runs the real stages (real parser process, stub provider) against a real workspace on disk, with
a recording StageContext instead of the database.
"""

import hashlib
import uuid

import pytest

from app.domain.enums import UploadKind, WorkflowType
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

    names = {path.rsplit("/", 1)[-1] for path in _snapshot(workspace)}
    assert names == {
        "validate.json",
        "pages.jsonl",
        "extract.json",
        "chunks.jsonl",
        "summary.json",
        "summary.md",
        "manifest.json",
    }  # no leftover temporary files
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
