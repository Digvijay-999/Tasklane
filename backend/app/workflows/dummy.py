"""A placeholder stage that only waits and reports progress (Phase 4).

Both workflow types run this single stage until Phase 5 (PDF) and Phase 6 (ZIP) replace it with
real stages. It exists to exercise the whole pipeline: outbox -> dispatcher -> Redis -> worker ->
claim -> stage events and progress -> succeeded.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.domain.enums import WorkflowType
from app.workflows.base import Stage, StageContext

DUMMY_STAGE_NAME = "dummy"


@dataclass
class DummyStage:
    duration_seconds: float
    steps: int = 5
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    name: str = DUMMY_STAGE_NAME

    def run(self, context: StageContext) -> None:
        for step in range(1, self.steps + 1):
            self.sleep(self.duration_seconds / self.steps)
            context.report_progress(round(step * 100 / self.steps))


def stages_for(workflow_type: WorkflowType, *, dummy_stage_seconds: float) -> list[Stage]:
    """The stages a job of this workflow type runs, in order."""
    # Phase 4: every workflow is the single placeholder stage.
    return [DummyStage(duration_seconds=dummy_stage_seconds)]
