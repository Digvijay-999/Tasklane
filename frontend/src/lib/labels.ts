// Words for machine values. The machine value stays visible where it matters (error classes,
// event types), these are the sentence-case labels around it.

import type { JobDetail, JobStatus, WorkflowType } from './api/types';

export const STATUS_LABEL: Record<JobStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  retry_wait: 'Waiting to retry',
  cancel_requested: 'Cancelling',
  succeeded: 'Succeeded',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export const WORKFLOW_LABEL: Record<WorkflowType, string> = {
  pdf_ingest: 'PDF ingestion',
  repo_index: 'Repository ZIP indexing',
};

export const WORKFLOW_DESCRIPTION: Record<WorkflowType, string> = {
  pdf_ingest: 'Extract the text of a PDF, split it into chunks and summarize it.',
  repo_index: "Index a source archive's text files, chunk them and summarize the repository.",
};

export const TRIGGER_LABEL: Record<string, string> = {
  initial: 'First run',
  auto_retry: 'Automatic retry',
  recovery: 'Recovery',
  manual_retry: 'Manual retry',
};

export const OUTCOME_LABEL: Record<string, string> = {
  succeeded: 'Succeeded',
  failed_transient: 'Failed, retryable',
  failed_permanent: 'Failed',
  cancelled: 'Cancelled',
  lease_expired: 'Worker lost',
};

const STAGE_LABEL: Record<string, string> = {
  validate_pdf: 'Validate',
  extract_text: 'Extract text',
  chunk_document: 'Chunk',
  summarize: 'Summarize',
  write_artifacts: 'Write artifacts',
  validate_archive: 'Validate archive',
  extract_files: 'Extract files',
  filter_files: 'Filter files',
  chunk_sources: 'Chunk sources',
};

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return '—';
  const known = STAGE_LABEL[stage];
  if (known) return known;
  const words = stage.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Why Retry is unavailable, and what to do instead (from the server's retry_blocked_reason). */
export const RETRY_BLOCKED: Record<NonNullable<JobDetail['retry_blocked_reason']>, string> = {
  not_failed: 'Only failed jobs can be retried.',
  input_error:
    'This job failed because of its input file, so retrying would fail the same way. Upload a corrected file and create a new job.',
  retry_limit_reached: 'This job has used all the attempts a single job may have. Create a new job to try again.',
};

export const CANCEL_BLOCKED: Record<NonNullable<JobDetail['cancel_blocked_reason']>, string> = {
  already_cancel_requested: 'Cancellation is already requested. The worker stops at the next stage boundary.',
  already_finished: 'The job has finished, so there is nothing to cancel.',
};

export const SKIP_REASON_LABEL: Record<string, string> = {
  excluded_dir: 'Excluded folder',
  hidden: 'Hidden file',
  system_file: 'System file',
  nested_archive: 'Nested archive',
  lockfile: 'Lockfile',
  minified: 'Minified',
  unsupported_type: 'Unsupported type',
  too_large: 'Too large',
  binary: 'Binary',
  empty: 'Empty',
  symlink: 'Symbolic link',
  special_file: 'Special file',
  path_too_long: 'Path too long',
  path_too_deep: 'Path too deep',
};
