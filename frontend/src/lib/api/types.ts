// Shapes of the Tasklane HTTP API (docs/api.md). Every string that came from an upload
// (filenames, archive paths, summaries) is untrusted: render it with <SafeText>.

export const JOB_STATUSES = [
  'queued',
  'running',
  'retry_wait',
  'cancel_requested',
  'succeeded',
  'failed',
  'cancelled',
] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const TERMINAL_STATUSES: readonly JobStatus[] = ['succeeded', 'failed', 'cancelled'];
export const isTerminal = (status: string): boolean =>
  (TERMINAL_STATUSES as readonly string[]).includes(status);

export type WorkflowType = 'pdf_ingest' | 'repo_index';

export interface Job {
  id: string;
  workflow_type: WorkflowType;
  status: JobStatus;
  current_stage: string | null;
  progress_percent: number;
  upload_id: string;
  input_sha256: string;
  options: Record<string, unknown>;
  attempt_count: number;
  max_attempts: number;
  manual_retry_count: number;
  retry_at: string | null;
  cancel_requested_at: string | null;
  error_class: string | null;
  error_summary: string | null;
  last_event_seq: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobListItem extends Job {
  upload_filename: string;
}

export interface JobList {
  items: JobListItem[];
  next_cursor: string | null;
}

export interface Attempt {
  attempt_no: number;
  trigger: 'initial' | 'auto_retry' | 'recovery' | 'manual_retry';
  worker_id: string;
  task_id: string | null;
  started_at: string;
  heartbeat_at: string;
  lease_expires_at: string;
  ended_at: string | null;
  outcome: 'succeeded' | 'failed_transient' | 'failed_permanent' | 'cancelled' | 'lease_expired' | null;
  error_class: string | null;
  error_summary: string | null;
}

export interface Artifact {
  id: string;
  artifact_type: 'summary' | 'manifest' | 'chunks';
  attempt_no: number;
  filename: string;
  size_bytes: number;
  content_type: string;
  sha256: string;
  created_at: string;
  download_url: string;
}

export interface JobDetail extends Job {
  upload: {
    id: string;
    original_filename: string;
    kind: string;
    size_bytes: number | null;
    sha256: string | null;
    status: string;
  };
  attempts: Attempt[];
  artifacts: Artifact[];
  lease: {
    attempt_no: number;
    worker_id: string;
    heartbeat_at: string;
    lease_expires_at: string;
    expired: boolean;
  } | null;
  can_cancel: boolean;
  cancel_blocked_reason: 'already_cancel_requested' | 'already_finished' | null;
  can_retry: boolean;
  retry_blocked_reason: 'not_failed' | 'input_error' | 'retry_limit_reached' | null;
}

export interface JobEvent {
  seq: number;
  event_type: string;
  attempt_no: number | null;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface EventPage {
  items: JobEvent[];
  next_after: number | null;
  last_seq: number;
}

export interface Overview {
  counts: Record<JobStatus, number>;
  total: number;
  oldest_queued: { job_id: string; queued_seconds: number } | null;
  queue: {
    broker: 'ok' | 'unavailable';
    pending_messages: number;
    scheduled_messages: number;
    failing_messages: number;
    max_dispatch_attempts: number;
    last_dispatch_error: string | null;
    oldest_pending_seconds: number | null;
  };
  database_time: string;
}

export interface WorkflowMeta {
  workflow_type: WorkflowType;
  upload_kind: string;
  extensions: string[];
  upload_content_type: string;
  accepted_content_types: string[];
  max_upload_bytes: number;
  signature_hex: string;
  stages: { name: string; weight: number }[];
}

export interface Meta {
  workflows: WorkflowMeta[];
  max_filename_length: number;
  upload_ttl_hours: number;
  fault_injection_enabled: boolean;
  max_fault_failures: number;
}

export interface Upload {
  id: string;
  original_filename: string;
  kind: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  status: string;
  created_at: string;
  expires_at: string;
}

export interface Ready {
  status: 'ready' | 'not_ready';
  checks: Record<string, 'ok' | 'unavailable'>;
}

export interface ErrorBody {
  error: { code: string; message: string; details: Record<string, unknown>; request_id: string | null };
}
