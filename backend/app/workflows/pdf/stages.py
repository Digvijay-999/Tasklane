"""The five stages of the pdf_ingest workflow.

Each stage reads its inputs from files written by earlier stages in the job's workspace
(work/{job_id}/), writes its outputs atomically to fixed paths there, and reports through the
StageContext. Running a stage twice is therefore safe: it recomputes the same output from the
same input and replaces the file (the stub provider is deterministic, and artifacts are upserted
by type).

    validate_pdf    upload file      -> validate.json
    extract_text    upload file      -> pages.jsonl, extract.json
    chunk_document  pages.jsonl      -> chunks.jsonl
    summarize       chunks.jsonl     -> summary.json        (provider calls recorded)
    write_artifacts all of the above -> artifacts/{job_id}/summary.md, manifest.json

Checkpoints (resume on retry): the first four stages declare `stage_version` (bump it whenever a
stage's output for the same input changes), `output_paths()` (every file the stage writes) and
`fingerprint_params()` (the settings its output depends on). The execution service uses them to
skip a stage whose earlier output still verifies; see app/domain/checkpoints.py. write_artifacts
has no checkpoint: it is cheap, and it must register the artifacts in every successful attempt.
Inside summarize, each chunk's summary is also cached in summaries/{chunk_id}.json, so a retry
after a failure on chunk 37 of 40 only sends chunks 37-40 to the provider.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.domain.enums import ArtifactType
from app.domain.errors import JobError
from app.providers.base import Provider
from app.storage.workspace import JobWorkspace
from app.workflows.base import JobInput, StageContext
from app.workflows.common import SummaryCaller, Throttle, register_artifact, verified_input_path
from app.workflows.pdf.chunking import CHUNKER_VERSION, ChunkingSettings, chunk_pages
from app.workflows.pdf.report import build_manifest, build_summary_markdown
from app.workflows.pdf.sandbox import ParserLimits, run_parser
from app.workflows.pdf.summarize import SummarySettings, estimated_calls, summarize_document
from app.workflows.pdf.text import (
    PAGE_WITH_TEXT_MIN_CHARS,
    has_enough_text,
    normalize_text,
    visible_chars,
)

logger = logging.getLogger(__name__)

VALIDATE_FILE = "validate.json"
PAGES_FILE = "pages.jsonl"
EXTRACT_FILE = "extract.json"
CHUNKS_FILE = "chunks.jsonl"
SUMMARY_FILE = "summary.json"
SUMMARY_ARTIFACT = "summary.md"
MANIFEST_ARTIFACT = "manifest.json"


@dataclass(frozen=True)
class PdfConfig:
    limits: ParserLimits
    validate_timeout_seconds: float
    extract_timeout_seconds: float
    summarize_timeout_seconds: float
    provider_timeout_seconds: float
    keep_alive_interval_seconds: float
    chunking: ChunkingSettings
    summary: SummarySettings


class ValidatePdfStage:
    name = "validate_pdf"
    weight = 5
    stage_version = 1

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(VALIDATE_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        return {
            "upload_sha256": job.upload.sha256,
            "upload_size": job.upload.size_bytes,
            "max_pages": self.config.limits.max_pages,
        }

    def run(self, context: StageContext) -> None:
        upload = context.job.upload
        path = verified_input_path(context, check_hash=True)
        workspace = context.workspace
        scratch = workspace.temp_path(workspace.work(VALIDATE_FILE))
        try:
            result = run_parser(
                "validate",
                path,
                scratch,
                limits=self.config.limits,
                timeout_seconds=self.config.validate_timeout_seconds,
                keep_alive=context.keep_alive,
                keep_alive_interval=self.config.keep_alive_interval_seconds,
            )
        finally:
            workspace.discard(scratch)
        workspace.write_json(
            workspace.work(VALIDATE_FILE),
            {
                "pages": int(result["pages"]),
                "owner_password_only": bool(result["owner_password_only"]),
                "pdf_header": str(result.get("pdf_header", "")),
                "input_sha256": upload.sha256,
            },
        )
        context.report_progress(100)


class ExtractTextStage:
    name = "extract_text"
    weight = 30
    stage_version = 1

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(PAGES_FILE), workspace.work(EXTRACT_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        return {
            "upload_sha256": job.upload.sha256,
            "max_pages": self.config.limits.max_pages,
            "max_chars": self.config.limits.max_chars,
        }

    def run(self, context: StageContext) -> None:
        path = verified_input_path(context, check_hash=False)
        workspace = context.workspace
        raw = workspace.temp_path(workspace.work("pages.raw.jsonl"))
        report = Throttle(context.report_progress)
        try:
            result = run_parser(
                "extract",
                path,
                raw,
                limits=self.config.limits,
                timeout_seconds=self.config.extract_timeout_seconds,
                keep_alive=context.keep_alive,
                keep_alive_interval=self.config.keep_alive_interval_seconds,
                on_progress=lambda done, total: report(round(done * 100 / max(total, 1))),
            )
            records = []
            with raw.open(encoding="utf-8") as handle:
                for line in handle:
                    page = json.loads(line)
                    text = normalize_text(str(page["text"]))
                    records.append(
                        {
                            "page": int(page["page"]),
                            "text": text,
                            "visible_chars": visible_chars(text),
                        }
                    )
        finally:
            workspace.discard(raw)

        pages = int(result["pages"])
        total_visible = sum(r["visible_chars"] for r in records)
        if not has_enough_text(total_visible, pages):
            raise JobError("pdf_no_text")
        with_text = sum(1 for r in records if r["visible_chars"] >= PAGE_WITH_TEXT_MIN_CHARS)
        workspace.write_jsonl(workspace.work(PAGES_FILE), records)
        workspace.write_json(
            workspace.work(EXTRACT_FILE),
            {
                "pages": pages,
                "pages_with_text": with_text,
                "pages_without_text": pages - with_text,
                "visible_chars": total_visible,
                "characters": sum(len(r["text"]) for r in records),
            },
        )
        context.report_progress(100)


class ChunkDocumentStage:
    name = "chunk_document"
    weight = 5
    stage_version = 1

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(CHUNKS_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        settings = self.config.chunking
        return {
            "upload_sha256": job.upload.sha256,  # part of every chunk id
            "chunker_version": CHUNKER_VERSION,
            "target_chars": settings.target_chars,
            "max_chars": settings.max_chars,
            "overlap_chars": settings.overlap_chars,
        }

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        pages = [(r["page"], r["text"]) for r in workspace.iter_jsonl(workspace.work(PAGES_FILE))]
        chunks = chunk_pages(
            pages, input_sha256=context.job.upload.sha256, settings=self.config.chunking
        )
        if not chunks:
            raise JobError("pdf_no_text")
        workspace.write_jsonl(workspace.work(CHUNKS_FILE), [c.to_record() for c in chunks])
        context.report_progress(100)


class SummarizeStage:
    name = "summarize"
    weight = 55
    stage_version = 1

    def __init__(self, config: PdfConfig, provider: Provider) -> None:
        self.config = config
        self.provider = provider

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        # The per-chunk cache files are not outputs: summary.json is built from them.
        return [workspace.work(SUMMARY_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        settings = self.config.summary
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "chunk_max_chars": settings.chunk_max_chars,
            "final_max_chars": settings.final_max_chars,
            "combine_max_input_chars": settings.combine_max_input_chars,
        }

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        chunks = list(workspace.iter_jsonl(workspace.work(CHUNKS_FILE)))
        caller = SummaryCaller(
            context,
            self.provider,
            stage_version=self.stage_version,
            chunk_sha256={c["chunk_id"]: c["sha256"] for c in chunks},
            expected_calls=estimated_calls(len(chunks), self.config.summary),
            stage_timeout_seconds=self.config.summarize_timeout_seconds,
            provider_timeout_seconds=self.config.provider_timeout_seconds,
        )
        document = summarize_document(
            [(c["chunk_id"], c["text"]) for c in chunks], caller, self.config.summary
        )
        workspace.write_json(
            workspace.work(SUMMARY_FILE),
            {
                "final": document.final,
                "sections": [
                    {
                        "chunk_id": c["chunk_id"],
                        "page_start": c["page_start"],
                        "page_end": c["page_end"],
                        "summary": section,
                    }
                    for c, section in zip(chunks, document.sections, strict=True)
                ],
                "combine_rounds": document.combine_rounds,
                "provider": self.provider.name,
                "model": self.provider.model,
                "calls": caller.done,
                "total_tokens": caller.total_tokens,
            },
        )
        caller.log_reuse()
        context.report_progress(100)


class WriteArtifactsStage:
    name = "write_artifacts"
    weight = 5

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        job = context.job
        validate = workspace.read_json(workspace.work(VALIDATE_FILE))
        extract = workspace.read_json(workspace.work(EXTRACT_FILE))
        summary = workspace.read_json(workspace.work(SUMMARY_FILE))
        chunk_count = sum(1 for _ in workspace.iter_jsonl(workspace.work(CHUNKS_FILE)))

        summary_path = workspace.artifact(SUMMARY_ARTIFACT)
        workspace.write_text(
            summary_path,
            build_summary_markdown(
                display_filename=job.upload.original_filename, extract=extract, summary=summary
            ),
        )
        summary_artifact = register_artifact(
            context,
            ArtifactType.SUMMARY,
            summary_path,
            SUMMARY_ARTIFACT,
            "text/markdown; charset=utf-8",
        )

        settings = self.config.chunking
        manifest_path = workspace.artifact(MANIFEST_ARTIFACT)
        workspace.write_json(
            manifest_path,
            build_manifest(
                job_id=str(job.job_id),
                workflow_type=job.workflow_type,
                upload={
                    "upload_id": str(job.upload.upload_id),
                    "filename": job.upload.original_filename,
                    "sha256": job.upload.sha256,
                    "size_bytes": job.upload.size_bytes,
                },
                validate=validate,
                extract=extract,
                chunking={
                    "method": "paragraph-sentence-greedy",
                    "chunker_version": CHUNKER_VERSION,
                    "target_chars": settings.target_chars,
                    "max_chars": settings.max_chars,
                    "overlap_chars": settings.overlap_chars,
                    "chunks": chunk_count,
                },
                summary=summary,
                artifacts=[
                    {
                        "type": summary_artifact.artifact_type,
                        "filename": summary_artifact.filename,
                        "size_bytes": summary_artifact.size_bytes,
                        "sha256": summary_artifact.sha256,
                    }
                ],
            ),
        )
        register_artifact(
            context, ArtifactType.MANIFEST, manifest_path, MANIFEST_ARTIFACT, "application/json"
        )
        context.report_progress(100)


def pdf_stages(config: PdfConfig, provider: Provider) -> list:  # type: ignore[type-arg]
    return [
        ValidatePdfStage(config),
        ExtractTextStage(config),
        ChunkDocumentStage(config),
        SummarizeStage(config, provider),
        WriteArtifactsStage(config),
    ]
