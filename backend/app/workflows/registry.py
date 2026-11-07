"""Which stages each workflow runs."""

from dataclasses import dataclass

from app.domain.enums import WorkflowType
from app.providers.base import Provider
from app.workflows.base import Stage
from app.workflows.dummy import DummyStage
from app.workflows.pdf.stages import PdfConfig, pdf_stages


@dataclass(frozen=True)
class WorkflowConfig:
    pdf: PdfConfig
    provider: Provider
    dummy_stage_seconds: float


def stages_for(workflow_type: WorkflowType, config: WorkflowConfig) -> list[Stage]:
    if workflow_type == WorkflowType.PDF_INGEST:
        return pdf_stages(config.pdf, config.provider)
    # repo_index keeps the placeholder stage until Phase 6.
    return [DummyStage(duration_seconds=config.dummy_stage_seconds)]
