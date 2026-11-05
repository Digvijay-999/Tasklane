"""The contract between the worker and stage code."""

from typing import Protocol


class StageContext(Protocol):
    """What a running stage can ask of the worker."""

    def report_progress(self, percent: int) -> None:
        """Record progress (0-100) within the current stage.

        Also renews the attempt's lease. If the worker no longer owns the job (its attempt was
        closed by someone else), this raises, which stops the stage; stages should not catch it.
        """
        ...


class Stage(Protocol):
    name: str

    def run(self, context: StageContext) -> None:
        """Do the stage's work. Raise to fail the attempt."""
        ...
