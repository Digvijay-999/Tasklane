"""Which stages each workflow runs."""

from dataclasses import dataclass

from app.domain.enums import WorkflowType
from app.providers.base import Provider
from app.workflows.base import Stage
from app.workflows.pdf.stages import PdfConfig, pdf_stages
from app.workflows.repo.stages import RepoConfig, repo_stages


@dataclass(frozen=True)
class WorkflowConfig:
    pdf: PdfConfig
    repo: RepoConfig
    provider: Provider


def stages_for(workflow_type: WorkflowType, config: WorkflowConfig) -> list[Stage]:
    if workflow_type == WorkflowType.PDF_INGEST:
        return pdf_stages(config.pdf, config.provider)
    if workflow_type == WorkflowType.REPO_INDEX:
        return repo_stages(config.repo, config.provider)
    raise ValueError(f"no stages for workflow {workflow_type!r}")
