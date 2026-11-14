import type { JobDetail, Meta } from '../lib/api/types';

export const META: Meta = {
  workflows: [
    {
      workflow_type: 'pdf_ingest',
      upload_kind: 'pdf',
      extensions: ['.pdf'],
      upload_content_type: 'application/pdf',
      accepted_content_types: ['application/pdf'],
      max_upload_bytes: 1000,
      signature_hex: '255044462d',
      stages: [{ name: 'validate_pdf', weight: 100 }],
    },
    {
      workflow_type: 'repo_index',
      upload_kind: 'zip',
      extensions: ['.zip'],
      upload_content_type: 'application/zip',
      accepted_content_types: ['application/zip'],
      max_upload_bytes: 2000,
      signature_hex: '504b0304',
      stages: [{ name: 'validate_archive', weight: 100 }],
    },
  ],
  max_filename_length: 255,
  upload_ttl_hours: 24,
  fault_injection_enabled: false,
  max_fault_failures: 20,
};

export function jobDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    id: '3f2a9c0e-5b7d-4e1f-8a6b-2c3d4e5f6a7b',
    workflow_type: 'pdf_ingest',
    status: 'running',
    current_stage: 'extract_text',
    progress_percent: 20,
    upload_id: 'u-1',
    input_sha256: 'a'.repeat(64),
    options: {},
    attempt_count: 1,
    max_attempts: 3,
    manual_retry_count: 0,
    retry_at: null,
    cancel_requested_at: null,
    error_class: null,
    error_summary: null,
    last_event_seq: 4,
    created_at: '2026-11-14T12:00:00Z',
    updated_at: '2026-11-14T12:00:01Z',
    started_at: '2026-11-14T12:00:01Z',
    finished_at: null,
    upload: { id: 'u-1', original_filename: 'report.pdf', kind: 'pdf', size_bytes: 10, sha256: null, status: 'ready' },
    attempts: [],
    artifacts: [],
    lease: null,
    can_cancel: true,
    cancel_blocked_reason: null,
    can_retry: false,
    retry_blocked_reason: 'not_failed',
    ...overrides,
  };
}

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}
