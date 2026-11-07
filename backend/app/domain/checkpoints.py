"""When a retried job may reuse a stage's earlier output instead of running the stage again (pure).

After a stage succeeds, the worker records a checkpoint: the stage's version, a fingerprint of
its inputs, and the size and SHA-256 of every file it wrote. On a later attempt of the same job,
the stage is skipped only if ALL of these hold:

1. no earlier stage of this attempt had to run (once one stage reruns, every later stage reruns
   too, so outputs are never mixed from different runs);
2. the stage's code version is unchanged (`stage_version`, bumped whenever its logic changes);
3. its input fingerprint is unchanged: the fingerprint covers the stage's settings and the hashes
   of every upstream output, so a change anywhere upstream changes it;
4. it wrote exactly the same set of files, and each one still exists with the recorded size and
   SHA-256 (nothing deleted or edited them since).

Anything else means the stage runs again.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class OutputFile:
    path: str  # relative to the storage root
    size_bytes: int
    sha256: str

    def to_record(self) -> dict[str, Any]:
        return {"path": self.path, "size_bytes": self.size_bytes, "sha256": self.sha256}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "OutputFile":
        return cls(str(record["path"]), int(record["size_bytes"]), str(record["sha256"]))


@dataclass(frozen=True)
class Checkpoint:
    stage: str
    stage_version: int
    input_fingerprint: str
    outputs: tuple[OutputFile, ...]


class ReuseDecision(StrEnum):
    REUSE = "reuse"
    NO_CHECKPOINT = "no_checkpoint"
    EARLIER_STAGE_RAN = "earlier_stage_ran"
    VERSION_CHANGED = "version_changed"
    INPUT_CHANGED = "input_changed"
    OUTPUTS_CHANGED = "outputs_changed"  # different file set, or a file missing or edited


def input_fingerprint(
    *,
    stage: str,
    stage_version: int,
    params: Mapping[str, Any],
    upstream: Sequence[OutputFile],
) -> str:
    """SHA-256 over everything a stage's output depends on (JSON with sorted keys)."""
    document = {
        "stage": stage,
        "stage_version": stage_version,
        "params": dict(params),
        "upstream": [f.to_record() for f in upstream],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reuse_decision(
    checkpoint: Checkpoint | None,
    *,
    stage_version: int,
    fingerprint: str,
    expected_paths: Sequence[str],
    earlier_stage_ran: bool,
    describe: Callable[[str], OutputFile | None],
) -> ReuseDecision:
    """Decide whether the stage can be skipped. `describe(path)` returns the file's current size
    and hash, or None if it doesn't exist (called last, because hashing reads the whole file)."""
    if checkpoint is None:
        return ReuseDecision.NO_CHECKPOINT
    if earlier_stage_ran:
        return ReuseDecision.EARLIER_STAGE_RAN
    if checkpoint.stage_version != stage_version:
        return ReuseDecision.VERSION_CHANGED
    if checkpoint.input_fingerprint != fingerprint:
        return ReuseDecision.INPUT_CHANGED
    if sorted(f.path for f in checkpoint.outputs) != sorted(expected_paths):
        return ReuseDecision.OUTPUTS_CHANGED
    for recorded in checkpoint.outputs:
        if describe(recorded.path) != recorded:
            return ReuseDecision.OUTPUTS_CHANGED
    return ReuseDecision.REUSE
