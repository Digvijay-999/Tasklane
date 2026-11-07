"""The six stages of the repo_index workflow.

Like the PDF stages, each reads files written by earlier stages in the job's workspace
(work/{job_id}/), writes its outputs atomically to fixed, generated paths there, and is safe to
run twice (same input -> byte-identical output).

    validate_archive  upload ZIP              -> archive.json   (sandboxed: central directory only)
    extract_files     upload ZIP + archive    -> files.bin, extract.json  (sandboxed)
    filter_files      archive + extracted     -> files.jsonl, filter.json
    chunk_sources     files.jsonl             -> chunks.jsonl
    summarize         chunks.jsonl            -> summary.json   (provider calls recorded)
    write_artifacts   all of the above        -> artifacts/{job_id}/summary.md, chunks.jsonl,
                                                 manifest.json

Names from inside the archive are DATA: they are never used to build a filesystem path. The
extracted bytes of all files live in ONE file, files.bin, at offsets recorded in extract.json.

The first five stages are checkpointed (stage_version / output_paths / fingerprint_params, see
app/domain/checkpoints.py), so a retry resumes after the last stage whose output still verifies.
write_artifacts is not: it must register the artifacts in every successful attempt.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from app.domain.enums import ArtifactType
from app.domain.errors import JobError
from app.providers.base import Provider
from app.storage.workspace import JobWorkspace
from app.workflows.base import JobInput, StageContext
from app.workflows.common import SummaryCaller, Throttle, register_artifact, verified_input_path
from app.workflows.pdf.chunking import ChunkingSettings
from app.workflows.pdf.summarize import (
    SummarySettings,
    reduce_calls_bound,
    reduce_summaries,
    summarize_document,
)
from app.workflows.repo.archive import SandboxRun, ZipLimits, extract_entries, inspect_archive
from app.workflows.repo.chunking import REPO_CHUNKER_VERSION, chunk_files, source_lines
from app.workflows.repo.filters import (
    FilterRules,
    SkipReason,
    decode_source,
    language_of,
    path_skip_reason,
)
from app.workflows.repo.report import build_manifest, build_summary_markdown

ARCHIVE_FILE = "archive.json"
BLOB_FILE = "files.bin"
EXTRACT_FILE = "extract.json"
FILES_FILE = "files.jsonl"
FILTER_FILE = "filter.json"
CHUNKS_FILE = "chunks.jsonl"
SUMMARY_FILE = "summary.json"
SUMMARY_ARTIFACT = "summary.md"
CHUNKS_ARTIFACT = "chunks.jsonl"
MANIFEST_ARTIFACT = "manifest.json"


@dataclass(frozen=True)
class RepoConfig:
    zip: ZipLimits
    excluded_dirs: frozenset[str]
    validate_timeout_seconds: float
    extract_timeout_seconds: float
    summarize_timeout_seconds: float
    provider_timeout_seconds: float
    keep_alive_interval_seconds: float
    chunking: ChunkingSettings
    summary: SummarySettings
    max_provider_calls: int

    @property
    def rules(self) -> FilterRules:
        return FilterRules(
            excluded_dirs=self.excluded_dirs,
            max_path_length=self.zip.max_path_length,
            max_path_depth=self.zip.max_path_depth,
            max_file_bytes=self.zip.max_file_bytes,
        )

    def rule_params(self) -> dict[str, Any]:
        """The settings the path rules depend on (for checkpoint fingerprints)."""
        return {
            "excluded_dirs": sorted(self.excluded_dirs),
            "max_path_length": self.zip.max_path_length,
            "max_path_depth": self.zip.max_path_depth,
            "max_file_bytes": self.zip.max_file_bytes,
        }


def _sandbox(context: StageContext, config: RepoConfig, timeout: float, on_progress=None):  # type: ignore[no-untyped-def]
    return SandboxRun(
        timeout_seconds=timeout,
        keep_alive=context.keep_alive,
        keep_alive_interval=config.keep_alive_interval_seconds,
        on_progress=on_progress,
    )


def _extract_candidates(entries: list[dict[str, Any]], config: RepoConfig) -> list[dict[str, Any]]:
    """Entries worth decompressing: regular files that pass every path rule and whose declared
    size is within the per-file limit. Everything else is never decompressed (a bomb hidden in
    node_modules/ or in a skipped archive costs nothing)."""
    rules = config.rules
    return [
        e
        for e in entries
        if e["kind"] == "file"
        and path_skip_reason(e["path"], rules, too_long=e["path_too_long"]) is None
        and e["size"] <= rules.max_file_bytes
    ]


class ValidateArchiveStage:
    name = "validate_archive"
    weight = 5
    stage_version = 1

    def __init__(self, config: RepoConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(ARCHIVE_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        return {
            "upload_sha256": job.upload.sha256,
            "upload_size": job.upload.size_bytes,
            "max_entries": self.config.zip.max_entries,
            "max_path_length": self.config.zip.max_path_length,
        }

    def run(self, context: StageContext) -> None:
        path = verified_input_path(context, check_hash=True)
        workspace = context.workspace
        scratch = workspace.temp_path(workspace.work(ARCHIVE_FILE))
        try:
            entries = inspect_archive(
                path,
                scratch,
                scratch.parent,  # names are resolved against the job's own work folder
                self.config.zip,
                _sandbox(context, self.config, self.config.validate_timeout_seconds),
            )
        finally:
            workspace.discard(scratch)
        workspace.write_json(
            workspace.work(ARCHIVE_FILE),
            {"entries": entries, "input_sha256": context.job.upload.sha256},
        )
        context.report_progress(100)


class ExtractFilesStage:
    name = "extract_files"
    weight = 15
    stage_version = 1

    def __init__(self, config: RepoConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(BLOB_FILE), workspace.work(EXTRACT_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        limits = self.config.zip
        return {
            "upload_sha256": job.upload.sha256,
            "max_total_bytes": limits.max_total_bytes,
            "max_compression_ratio": limits.max_compression_ratio,
            "ratio_check_min_bytes": limits.ratio_check_min_bytes,
            **self.config.rule_params(),  # they decide which entries are extracted
        }

    def run(self, context: StageContext) -> None:
        path = verified_input_path(context, check_hash=False)
        workspace = context.workspace
        entries = workspace.read_json(workspace.work(ARCHIVE_FILE))["entries"]
        candidates = _extract_candidates(entries, self.config)
        blob = workspace.temp_path(workspace.work(BLOB_FILE))
        request = workspace.temp_path(workspace.work("extract.request.json"))
        index_file = workspace.temp_path(workspace.work("extract.index.json"))
        report = Throttle(context.report_progress)
        try:
            results = extract_entries(
                path,
                [e["index"] for e in candidates],
                request=request,
                blob=blob,
                index_file=index_file,
                limits=self.config.zip,
                run=_sandbox(
                    context,
                    self.config,
                    self.config.extract_timeout_seconds,
                    lambda done, total: report(min(99, round(done * 100 / max(total, 1)))),
                ),
            )
            records = []
            with blob.open("rb") as handle:
                for entry in candidates:
                    found = results[entry["index"]]
                    if found is None:
                        records.append({"index": entry["index"], "too_large": True})
                        continue
                    handle.seek(found["offset"])
                    data = handle.read(found["size"])
                    records.append(
                        {
                            "index": entry["index"],
                            "offset": found["offset"],
                            "size": found["size"],
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    )
            workspace.commit(blob, workspace.work(BLOB_FILE))
        finally:
            for scratch in (blob, request, index_file):
                workspace.discard(scratch)
        workspace.write_json(
            workspace.work(EXTRACT_FILE),
            {"files": records, "bytes": sum(r.get("size", 0) for r in records)},
        )
        context.report_progress(100)


class FilterFilesStage:
    name = "filter_files"
    weight = 5
    stage_version = 1

    def __init__(self, config: RepoConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(FILES_FILE), workspace.work(FILTER_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        return self.config.rule_params()

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        rules = self.config.rules
        entries = workspace.read_json(workspace.work(ARCHIVE_FILE))["entries"]
        extracted = {
            r["index"]: r for r in workspace.read_json(workspace.work(EXTRACT_FILE))["files"]
        }
        # A repository with more text than this can never be summarized within the call limit
        # (each call sees at most chunk_max_chars), so stop reading early.
        max_chars = self.config.max_provider_calls * self.config.chunking.max_chars
        files = sorted((e for e in entries if e["kind"] != "dir"), key=lambda e: e["path"])
        report = Throttle(context.report_progress)
        included: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        total_chars = 0
        blob_path = workspace.absolute(workspace.work(BLOB_FILE))
        try:
            with blob_path.open("rb") as blob:
                for done, entry in enumerate(files, start=1):
                    reason, text = self._classify(entry, rules, extracted, blob)
                    report(min(99, round(done * 100 / len(files))))
                    if reason is not None:
                        skipped.append({"path": entry["path"], "reason": str(reason)})
                        continue
                    total_chars += len(text)
                    if total_chars > max_chars:
                        raise JobError("repo_too_large", limit=self.config.max_provider_calls)
                    record = {
                        "path": entry["path"],
                        "language": language_of(entry["path"]),
                        "bytes": extracted[entry["index"]]["size"],
                        "lines": len(source_lines(text)),
                        "sha256": extracted[entry["index"]]["sha256"],
                    }
                    included.append(record)
                    sources.append({**record, "text": text})
            if not included:
                raise JobError("repo_no_sources", files=len(files))
        except JobError:
            # A permanent failure: no retry will read files.bin again, so don't leave up to
            # ZIP_MAX_TOTAL_BYTES of extracted data behind (retention comes in Phase 12).
            workspace.discard(blob_path)
            raise

        reasons: dict[str, int] = {}
        for item in skipped:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        languages: dict[str, dict[str, int]] = {}
        for record in included:
            stats = languages.setdefault(record["language"], {"files": 0, "lines": 0, "bytes": 0})
            stats["files"] += 1
            stats["lines"] += record["lines"]
            stats["bytes"] += record["bytes"]
        workspace.write_jsonl(workspace.work(FILES_FILE), sources)
        workspace.write_json(
            workspace.work(FILTER_FILE),
            {
                "included": included,
                "skipped": skipped,
                "skip_reasons": dict(sorted(reasons.items())),
                "languages": dict(sorted(languages.items())),
                "totals": {
                    "files": len(files),
                    "included": len(included),
                    "skipped": len(skipped),
                    "included_bytes": sum(r["bytes"] for r in included),
                    "included_lines": sum(r["lines"] for r in included),
                },
            },
        )
        context.report_progress(100)

    @staticmethod
    def _classify(  # type: ignore[no-untyped-def]
        entry: dict[str, Any], rules: FilterRules, extracted: dict[int, Any], blob
    ) -> tuple[SkipReason | None, str]:
        if entry["kind"] == "symlink":
            return SkipReason.SYMLINK, ""
        if entry["kind"] == "special":
            return SkipReason.SPECIAL_FILE, ""
        reason = path_skip_reason(entry["path"], rules, too_long=entry["path_too_long"])
        if reason is not None:
            return reason, ""
        found = extracted.get(entry["index"])
        if entry["size"] > rules.max_file_bytes or found is None or found.get("too_large"):
            # Declared too large (never decompressed), or really too large while decompressing.
            return SkipReason.TOO_LARGE, ""
        blob.seek(found["offset"])
        data = blob.read(found["size"])
        if hashlib.sha256(data).hexdigest() != found["sha256"]:
            raise RuntimeError("extracted file changed on disk")
        return decode_source(data, str(language_of(entry["path"])))


class ChunkSourcesStage:
    name = "chunk_sources"
    weight = 5
    stage_version = 1

    def __init__(self, config: RepoConfig) -> None:
        self.config = config

    def output_paths(self, workspace: JobWorkspace) -> list[str]:
        return [workspace.work(CHUNKS_FILE)]

    def fingerprint_params(self, job: JobInput) -> dict[str, Any]:
        settings = self.config.chunking
        return {
            "upload_sha256": job.upload.sha256,  # part of every chunk id
            "chunker_version": REPO_CHUNKER_VERSION,
            "target_chars": settings.target_chars,
            "max_chars": settings.max_chars,
            "overlap_chars": settings.overlap_chars,
        }

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        files = ((f["path"], f["text"]) for f in workspace.iter_jsonl(workspace.work(FILES_FILE)))
        chunks = chunk_files(
            files, input_sha256=context.job.upload.sha256, settings=self.config.chunking
        )
        workspace.write_jsonl(workspace.work(CHUNKS_FILE), [c.to_record() for c in chunks])
        context.report_progress(100)


def planned_calls(chunks_per_file: list[int], settings: SummarySettings) -> int:
    """An upper bound on the provider calls the summarize stage makes: every chunk, the combine
    rounds of every multi-chunk file, and the combine rounds of the overview."""
    per_file = sum(
        k + reduce_calls_bound(k, settings.chunk_max_chars, settings) for k in chunks_per_file
    )
    return per_file + reduce_calls_bound(len(chunks_per_file), settings.final_max_chars, settings)


class SummarizeStage:
    name = "summarize"
    weight = 65
    stage_version = 1

    def __init__(self, config: RepoConfig, provider: Provider) -> None:
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
        settings = self.config.summary
        chunks = list(workspace.iter_jsonl(workspace.work(CHUNKS_FILE)))
        by_file: dict[str, list[dict[str, Any]]] = {}
        for chunk in chunks:
            by_file.setdefault(chunk["path"], []).append(chunk)
        planned = planned_calls([len(c) for c in by_file.values()], settings)
        if planned > self.config.max_provider_calls:  # checked before any call is made
            raise JobError("repo_too_large", limit=self.config.max_provider_calls)
        caller = SummaryCaller(
            context,
            self.provider,
            stage_version=self.stage_version,
            chunk_sha256={c["chunk_id"]: c["sha256"] for c in chunks},
            expected_calls=planned,
            stage_timeout_seconds=self.config.summarize_timeout_seconds,
            provider_timeout_seconds=self.config.provider_timeout_seconds,
        )
        files = []
        for path, file_chunks in by_file.items():
            document = summarize_document(
                [(c["chunk_id"], c["text"]) for c in file_chunks], caller, settings
            )
            files.append(
                {
                    "path": path,
                    "chunks": len(file_chunks),
                    "summary": document.final,
                    "combine_rounds": document.combine_rounds,
                }
            )
        if len(files) == 1:
            overview, rounds = files[0]["summary"], 0
        else:
            # "path: summary", cut to final_max_chars so every combine batch still holds at
            # least two items (the guarantee that map-reduce terminates).
            items = [f"{f['path']}: {f['summary']}"[: settings.final_max_chars] for f in files]
            overview, rounds = reduce_summaries(items, caller, settings)
        workspace.write_json(
            workspace.work(SUMMARY_FILE),
            {
                "overview": overview,
                "files": files,
                "combine_rounds": rounds,
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

    def __init__(self, config: RepoConfig) -> None:
        self.config = config

    def run(self, context: StageContext) -> None:
        workspace = context.workspace
        job = context.job
        archive = workspace.read_json(workspace.work(ARCHIVE_FILE))
        filtered = workspace.read_json(workspace.work(FILTER_FILE))
        summary = workspace.read_json(workspace.work(SUMMARY_FILE))
        chunks = [
            {
                "chunk_id": c["chunk_id"],
                "path": c["path"],
                "line_start": c["line_start"],
                "line_end": c["line_end"],
                "sha256": c["sha256"],
                "text": c["text"],
            }
            for c in workspace.iter_jsonl(workspace.work(CHUNKS_FILE))
        ]

        summary_path = workspace.artifact(SUMMARY_ARTIFACT)
        workspace.write_text(
            summary_path,
            build_summary_markdown(
                display_filename=job.upload.original_filename,
                filtered=filtered,
                summary=summary,
                chunk_count=len(chunks),
            ),
        )
        written = [
            register_artifact(
                context,
                ArtifactType.SUMMARY,
                summary_path,
                SUMMARY_ARTIFACT,
                "text/markdown; charset=utf-8",
            )
        ]
        chunks_path = workspace.artifact(CHUNKS_ARTIFACT)
        workspace.write_jsonl(chunks_path, chunks, ensure_ascii=True)
        written.append(
            register_artifact(
                context, ArtifactType.CHUNKS, chunks_path, CHUNKS_ARTIFACT, "application/x-ndjson"
            )
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
                archive=archive,
                filtered=filtered,
                chunking={
                    "method": "line-greedy",
                    "chunker_version": REPO_CHUNKER_VERSION,
                    "target_chars": settings.target_chars,
                    "max_chars": settings.max_chars,
                    "overlap_chars": settings.overlap_chars,
                    "chunks": len(chunks),
                },
                summary=summary,
                limits={
                    **self.config.zip.to_record(),
                    "excluded_dirs": sorted(self.config.excluded_dirs),
                    "max_provider_calls": self.config.max_provider_calls,
                },
                artifacts=[
                    {
                        "type": a.artifact_type,
                        "filename": a.filename,
                        "size_bytes": a.size_bytes,
                        "sha256": a.sha256,
                    }
                    for a in written
                ],
            ),
            ensure_ascii=True,
        )
        register_artifact(
            context, ArtifactType.MANIFEST, manifest_path, MANIFEST_ARTIFACT, "application/json"
        )
        context.report_progress(100)


def repo_stages(config: RepoConfig, provider: Provider) -> list:  # type: ignore[type-arg]
    return [
        ValidateArchiveStage(config),
        ExtractFilesStage(config),
        FilterFilesStage(config),
        ChunkSourcesStage(config),
        SummarizeStage(config, provider),
        WriteArtifactsStage(config),
    ]
