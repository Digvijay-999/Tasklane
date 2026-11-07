"""A stand-in stage for tests: it only waits and reports progress.

Phases 4-5 ran it as the repo_index workflow; since Phase 6 it is used only by tests that need
a stage without real work (execution, dispatcher).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.workflows.base import StageContext

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
