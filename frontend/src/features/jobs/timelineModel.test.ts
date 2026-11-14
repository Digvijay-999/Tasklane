import { describe, expect, it } from 'vitest';
import type { JobEvent } from '../../lib/api/types';
import { buildTimeline } from './timelineModel';

const STAGES = ['validate_pdf', 'extract_text', 'chunk_document', 'summarize', 'write_artifacts'];
let seq = 0;
const t = (s: number) => new Date(Date.UTC(2026, 10, 14, 12, 0, s)).toISOString();
function ev(event_type: string, payload: Record<string, unknown>, attempt_no: number | null, at: number): JobEvent {
  return { seq: ++seq, event_type, attempt_no, created_at: t(at), payload };
}
const stage = (attempt: number, name: string, at: number, ms: number) => [
  ev('stage.started', { stage: name }, attempt, at),
  ev('stage.completed', { stage: name, duration_ms: ms }, attempt, at + 1),
];
const states = (timeline: ReturnType<typeof buildTimeline>, lane: number) =>
  timeline.lanes[lane]!.cells.map((c) => c.state);

describe('buildTimeline', () => {
  it('shows a transient failure, the retry wait and a resumed second attempt', () => {
    seq = 0;
    const events = [
      ev('job.created', {}, null, 0),
      ev('job.status_changed', { from: 'queued', to: 'running' }, 1, 1),
      ev('attempt.started', { trigger: 'initial', worker_id: 'w1', attempt_no: 1 }, 1, 1),
      ...stage(1, 'validate_pdf', 1, 300),
      ...stage(1, 'extract_text', 2, 900),
      ...stage(1, 'chunk_document', 3, 50),
      ev('stage.started', { stage: 'summarize' }, 1, 4),
      ev('stage.failed', { stage: 'summarize', error_class: 'injected_transient', retryable: true }, 1, 5),
      ev('job.status_changed', { from: 'running', to: 'retry_wait', error_class: 'injected_transient', retry_at: t(9) }, 1, 5),
      ev('job.status_changed', { from: 'retry_wait', to: 'running' }, 2, 9),
      ev('attempt.started', { trigger: 'auto_retry', worker_id: 'w1', attempt_no: 2 }, 2, 9),
      ev('stage.skipped', { stage: 'validate_pdf', reason: 'checkpoint_reused' }, 2, 9),
      ev('stage.skipped', { stage: 'extract_text', reason: 'checkpoint_reused' }, 2, 9),
      ev('stage.skipped', { stage: 'chunk_document', reason: 'checkpoint_reused' }, 2, 9),
      ...stage(2, 'summarize', 10, 1200),
      ...stage(2, 'write_artifacts', 12, 40),
      ev('job.status_changed', { from: 'running', to: 'succeeded' }, 2, 13),
    ];
    const timeline = buildTimeline(events, STAGES);
    expect(timeline.lanes).toHaveLength(2);
    expect(states(timeline, 0)).toEqual(['done', 'done', 'done', 'failed', 'pending']);
    expect(timeline.lanes[0]!.cells[3]).toMatchObject({ errorClass: 'injected_transient', retryable: true, durationMs: 1000 });
    expect(timeline.lanes[0]!.outcome).toBe('retry');
    expect(timeline.lanes[0]!.after).toEqual([
      { kind: 'retry_wait', at: t(5), errorClass: 'injected_transient', retryAt: t(9), waitedMs: 4000 },
    ]);
    expect(states(timeline, 1)).toEqual(['reused', 'reused', 'reused', 'done', 'done']);
    expect(timeline.lanes[1]!.trigger).toBe('auto_retry');
    expect(timeline.final).toMatchObject({ status: 'succeeded', cancelSuperseded: false });
    expect(timeline.longestStageMs).toBe(1200);
  });

  it('marks the running stage interrupted when the lease expires, then recovery retries', () => {
    seq = 0;
    const events = [
      ev('attempt.started', { trigger: 'initial', worker_id: 'w1', attempt_no: 1 }, 1, 0),
      ...stage(1, 'validate_pdf', 0, 100),
      ev('stage.started', { stage: 'extract_text' }, 1, 1),
      ev('stage.progress', { stage: 'extract_text', percent: 40 }, 1, 2),
      ev('attempt.lease_expired', { attempt_no: 1, worker_id: 'w1', last_heartbeat_at: t(2), lease_expired_at: t(62) }, 1, 63),
      ev('job.status_changed', { from: 'running', to: 'retry_wait', reason: 'lease_expired', error_class: 'lease_expired', attempt_no: 1 }, null, 63),
      ev('attempt.started', { trigger: 'recovery', worker_id: 'w2', attempt_no: 2 }, 2, 66),
    ];
    const timeline = buildTimeline(events, STAGES);
    expect(states(timeline, 0)).toEqual(['done', 'interrupted', 'pending', 'pending', 'pending']);
    expect(timeline.lanes[0]!.outcome).toBe('lease_expired');
    expect(timeline.lanes[0]!.after.map((g) => g.kind)).toEqual(['lease_expired', 'retry_wait']);
    expect(timeline.lanes[1]!.trigger).toBe('recovery');
    expect(timeline.final).toBeNull();
  });

  it('shows a cancel request and the stages that never ran', () => {
    seq = 0;
    const events = [
      ev('attempt.started', { trigger: 'initial', worker_id: 'w1', attempt_no: 1 }, 1, 0),
      ...stage(1, 'validate_pdf', 0, 100),
      ev('stage.started', { stage: 'extract_text' }, 1, 1),
      ev('job.status_changed', { from: 'running', to: 'cancel_requested', requested_by: 'api' }, null, 2),
      ev('stage.completed', { stage: 'extract_text', duration_ms: 2000 }, 1, 3),
      ev('job.status_changed', { from: 'cancel_requested', to: 'cancelled', before_stage: 'chunk_document', attempt_no: 1 }, 1, 3),
    ];
    const timeline = buildTimeline(events, STAGES);
    expect(timeline.lanes[0]!.cancelRequestedAt).toBe(t(2));
    expect(states(timeline, 0)).toEqual(['done', 'done', 'not_run', 'not_run', 'not_run']);
    expect(timeline.final).toMatchObject({ status: 'cancelled', beforeStage: 'chunk_document' });
  });

  it('reopens on a manual retry and records the new budget', () => {
    seq = 0;
    const events = [
      ev('attempt.started', { trigger: 'initial', worker_id: 'w1', attempt_no: 1 }, 1, 0),
      ev('stage.started', { stage: 'validate_pdf' }, 1, 0),
      ev('stage.failed', { stage: 'validate_pdf', error_class: 'internal_error', retryable: false }, 1, 1),
      ev('job.status_changed', { from: 'running', to: 'failed', error_class: 'internal_error' }, 1, 1),
      ev('job.status_changed', { from: 'failed', to: 'queued', manual_retry_count: 1 }, null, 5),
      ev('job.manual_retry', { manual_retry_count: 1, previous_error_class: 'internal_error', attempt_count: 1, max_attempts: 4 }, null, 5),
    ];
    const timeline = buildTimeline(events, STAGES);
    expect(timeline.final).toBeNull();
    expect(timeline.status).toBe('queued');
    expect(timeline.lanes[0]!.outcome).toBe('failed');
    expect(timeline.lanes[0]!.after).toEqual([{ kind: 'manual_retry', at: t(5), count: 1, firstAttempt: 2, maxAttempts: 4 }]);
  });

  it('records a cancellation that the job outran', () => {
    seq = 0;
    const events = [
      ev('attempt.started', { trigger: 'initial', worker_id: 'w1', attempt_no: 1 }, 1, 0),
      ev('job.status_changed', { from: 'running', to: 'cancel_requested' }, null, 1),
      ev('job.status_changed', { from: 'cancel_requested', to: 'succeeded' }, 1, 2),
      ev('job.cancel_superseded', { outcome: 'succeeded', reason: 'completed_before_cancel' }, 1, 2),
    ];
    expect(buildTimeline(events, STAGES).final).toMatchObject({ status: 'succeeded', cancelSuperseded: true });
  });

  it('distinguishes a job cancelled while waiting from one stopped mid-run', () => {
    seq = 0;
    const timeline = buildTimeline(
      [ev('job.created', {}, null, 0), ev('job.status_changed', { from: 'queued', to: 'cancelled', outbox_removed: true }, null, 1)],
      STAGES,
    );
    expect(timeline.lanes).toHaveLength(0);
    expect(timeline.cancelledWhileWaiting).toBe(true);
  });
});
