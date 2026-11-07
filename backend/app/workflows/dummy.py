"""A placeholder stage that only waits and reports progress.

The repo_index workflow runs this single stage until Phase 6 replaces it with real stages. It
exercises the whole pipeline: outbox -> dispatcher -> Redis -> worker -> claim -> stage events and
progress -> succeeded.
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
