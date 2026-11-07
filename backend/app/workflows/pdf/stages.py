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
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.enums import ArtifactType, ProviderCallPurpose
from app.domain.errors import JobError
from app.providers.base import Provider, ProviderCallRecord, SummarizeOptions
from app.storage.workspace import sha256_file
from app.workflows.base import ArtifactOutput, StageContext
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


def _input_path(context: StageContext):  # type: ignore[no-untyped-def]
    """The uploaded file, after checking it still exists and hasn't changed."""
    upload = context.job.upload
    path = context.workspace.absolute(upload.storage_path)
    if not path.is_file():
        raise JobError("input_unavailable")
    return path


class _Throttle:
    """Report progress at most every `seconds` or every `step` percent, whichever comes first."""

    def __init__(self, report: Callable[[int], None], *, seconds: float = 2.0, step: int = 5):
        self.report = report
        self.seconds = seconds
        self.step = step
        self.last_percent = -100
        self.last_time = 0.0

    def __call__(self, percent: int) -> None:
        now = time.monotonic()
        if percent - self.last_percent >= self.step or now - self.last_time >= self.seconds:
            self.report(percent)
            self.last_percent, self.last_time = percent, now


class ValidatePdfStage:
    name = "validate_pdf"
    weight = 5

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

    def run(self, context: StageContext) -> None:
        upload = context.job.upload
        path = _input_path(context)
        if sha256_file(path) != upload.sha256:
            logger.error(
                "uploaded file no longer matches its hash", extra={"upload": str(upload.upload_id)}
            )
            raise JobError("input_unavailable")
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

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

    def run(self, context: StageContext) -> None:
        path = _input_path(context)
        workspace = context.workspace
        raw = workspace.temp_path(workspace.work("pages.raw.jsonl"))
        report = _Throttle(context.report_progress)
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

    def __init__(self, config: PdfConfig) -> None:
        self.config = config

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

    def __init__(self, config: PdfConfig, provider: Provider) -> None:
        self.config = config
        self.provider = provider

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        chunks = list(workspace.iter_jsonl(workspace.work(CHUNKS_FILE)))
        expected = max(1, estimated_calls(len(chunks), self.config.summary))
        deadline = time.monotonic() + self.config.summarize_timeout_seconds
        calls = {"done": 0, "tokens": 0, "tokens_known": True}

        def call(
            text: str, purpose: ProviderCallPurpose, chunk_id: str | None, max_chars: int
        ) -> str:
            if time.monotonic() > deadline:
                raise JobError("stage_timeout", limit=int(self.config.summarize_timeout_seconds))
            options = SummarizeOptions(
                purpose=purpose,
                max_output_chars=max_chars,
                timeout_seconds=self.config.provider_timeout_seconds,
            )
            started = time.perf_counter()
            try:
                result = self.provider.summarize(text, options)
            except Exception as exc:
                context.record_provider_call(
                    ProviderCallRecord(
                        purpose=purpose,
                        provider=self.provider.name,
                        model=self.provider.model,
                        latency_ms=round((time.perf_counter() - started) * 1000),
                        success=False,
                        input_chars=len(text),
                        chunk_id=chunk_id,
                        # Never None: the table requires an error class for failed calls.
                        error_class=getattr(exc, "error_class", None) or "internal_error",
                    )
                )
                raise
            # A provider may ignore max_output_chars; enforce it here, so every combine round
            # really shrinks the text (otherwise map-reduce could loop until the stage timeout).
            text_out = result.text[:max_chars]
            # Recording the call also renews the lease, so each call's timeout (at most half the
            # lease) bounds the longest time between renewals.
            context.record_provider_call(
                ProviderCallRecord(
                    purpose=purpose,
                    provider=result.provider,
                    model=result.model,
                    latency_ms=result.latency_ms,
                    success=True,
                    input_chars=len(text),
                    output_chars=len(text_out),
                    chunk_id=chunk_id,
                    usage=result.usage,
                )
            )
            calls["done"] += 1
            if result.usage is not None and result.usage.total_tokens is not None:
                calls["tokens"] += result.usage.total_tokens
            else:
                calls["tokens_known"] = False
            context.report_progress(min(99, round(calls["done"] * 100 / expected)))
            return text_out

        document = summarize_document(
            [(c["chunk_id"], c["text"]) for c in chunks], call, self.config.summary
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
                "calls": calls["done"],
                "total_tokens": calls["tokens"] if calls["tokens_known"] else None,
            },
        )
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
        summary_artifact = self._register(
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
        self._register(
            context, ArtifactType.MANIFEST, manifest_path, MANIFEST_ARTIFACT, "application/json"
        )
        context.report_progress(100)

    @staticmethod
    def _register(
        context: StageContext, artifact_type: str, path: str, filename: str, content_type: str
    ) -> ArtifactOutput:
        artifact = ArtifactOutput(
            artifact_type=str(artifact_type),
            relative_path=path,
            filename=filename,
            content_type=content_type,
            size_bytes=context.workspace.size(path),
            sha256=context.workspace.sha256(path),
        )
        context.save_artifact(artifact)
        return artifact


def pdf_stages(config: PdfConfig, provider: Provider) -> list:  # type: ignore[type-arg]
    return [
        ValidatePdfStage(config),
        ExtractTextStage(config),
        ChunkDocumentStage(config),
        SummarizeStage(config, provider),
        WriteArtifactsStage(config),
    ]
