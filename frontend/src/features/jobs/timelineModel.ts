import type { JobEvent } from '../../lib/api/types';

/**
 * Turns a job's events (ordered by seq) into the timeline: one lane per attempt, one cell per
 * stage (stage list from GET /v1/meta), plus what happened between attempts and the outcome.
 * Pure: the same events always give the same timeline (unit-tested).
 */

export type CellState = 'pending' | 'running' | 'done' | 'reused' | 'failed' | 'interrupted' | 'not_run';

export interface StageCell {
  stage: string;
  state: CellState;
  startedAt: string | null;
  durationMs: number | null;
  percent: number | null;
  errorClass: string | null;
  retryable: boolean | null;
}

export type Gap =
  | { kind: 'retry_wait'; at: string; errorClass: string | null; retryAt: string | null; waitedMs: number | null }
  | { kind: 'lease_expired'; at: string; workerId: string | null; lastHeartbeatAt: string | null }
  | { kind: 'manual_retry'; at: string; count: number | null; firstAttempt: number | null; maxAttempts: number | null }
  | { kind: 'requeued'; at: string };

export type LaneOutcome = 'succeeded' | 'failed' | 'retry' | 'cancelled' | 'lease_expired' | null;

export interface Lane {
  attemptNo: number;
  trigger: string | null;
  workerId: string | null;
  startedAt: string | null;
  endedAt: string | null;
  outcome: LaneOutcome;
  cancelRequestedAt: string | null;
  cells: StageCell[];
  after: Gap[];
}

export interface FinalOutcome {
  status: 'succeeded' | 'failed' | 'cancelled';
  at: string;
  errorClass: string | null;
  beforeStage: string | null; // cancelled at a stage boundary
  gaveUpAfter: number | null;
  cancelSuperseded: boolean;
}

export interface Timeline {
  createdAt: string | null;
  before: Gap[]; // things that happened before the first attempt (e.g. a lost message)
  lanes: Lane[];
  final: FinalOutcome | null;
  status: string | null; // latest status according to the events
  cancelledWhileWaiting: boolean;
  longestStageMs: number;
}

const str = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const num = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const ms = (from: string | null, to: string | null): number | null =>
  from && to ? Math.max(0, Date.parse(to) - Date.parse(from)) : null;

function emptyCell(stage: string): StageCell {
  return { stage, state: 'pending', startedAt: null, durationMs: null, percent: null, errorClass: null, retryable: null };
}

export function buildTimeline(events: readonly JobEvent[], stages: readonly string[]): Timeline {
  const timeline: Timeline = {
    createdAt: null,
    before: [],
    lanes: [],
    final: null,
    status: null,
    cancelledWhileWaiting: false,
    longestStageMs: 0,
  };
  const lanes = new Map<number, Lane>();

  const lane = (attemptNo: number): Lane => {
    let found = lanes.get(attemptNo);
    if (!found) {
      found = {
        attemptNo,
        trigger: null,
        workerId: null,
        startedAt: null,
        endedAt: null,
        outcome: null,
        cancelRequestedAt: null,
        cells: stages.map(emptyCell),
        after: [],
      };
      lanes.set(attemptNo, found);
      timeline.lanes.push(found);
      timeline.lanes.sort((a, b) => a.attemptNo - b.attemptNo);
    }
    return found;
  };
  const cell = (owner: Lane, stage: string): StageCell => {
    let found = owner.cells.find((c) => c.stage === stage);
    if (!found) {
      found = emptyCell(stage); // a stage the metadata didn't list: still shown, at the end
      owner.cells.push(found);
    }
    return found;
  };
  const lastLane = (): Lane | undefined => timeline.lanes[timeline.lanes.length - 1];
  const addGap = (gap: Gap) => {
    const owner = lastLane();
    (owner ? owner.after : timeline.before).push(gap);
  };

  for (const event of events) {
    const p = event.payload;
    const attemptNo = event.attempt_no ?? num(p.attempt_no);
    const at = event.created_at;
    switch (event.event_type) {
      case 'job.created':
        timeline.createdAt = at;
        timeline.status = 'queued';
        break;
      case 'attempt.started': {
        const owner = lane(attemptNo ?? timeline.lanes.length + 1);
        owner.trigger = str(p.trigger);
        owner.workerId = str(p.worker_id);
        owner.startedAt = at;
        break;
      }
      case 'stage.started':
      case 'stage.progress':
      case 'stage.completed':
      case 'stage.skipped':
      case 'stage.failed': {
        const stage = str(p.stage);
        if (!stage || attemptNo === null) break;
        const target = cell(lane(attemptNo), stage);
        if (event.event_type === 'stage.started') {
          Object.assign(target, emptyCell(stage), { state: 'running', startedAt: at, percent: 0 });
        } else if (event.event_type === 'stage.progress') {
          if (target.state === 'pending') Object.assign(target, { state: 'running', startedAt: at });
          target.percent = num(p.percent);
        } else if (event.event_type === 'stage.completed') {
          target.state = 'done';
          target.durationMs = num(p.duration_ms) ?? ms(target.startedAt, at);
          target.percent = 100;
        } else if (event.event_type === 'stage.skipped') {
          target.state = 'reused';
        } else {
          target.state = 'failed';
          target.durationMs = ms(target.startedAt, at);
          target.errorClass = str(p.error_class);
          target.retryable = typeof p.retryable === 'boolean' ? p.retryable : null;
        }
        break;
      }
      case 'attempt.lease_expired': {
        const owner = lane(attemptNo ?? lastLane()?.attemptNo ?? 1);
        owner.outcome = 'lease_expired';
        owner.endedAt = str(p.lease_expired_at) ?? at;
        owner.cells.forEach((c) => {
          if (c.state === 'running') Object.assign(c, { state: 'interrupted', durationMs: ms(c.startedAt, owner.endedAt) });
        });
        owner.after.push({ kind: 'lease_expired', at, workerId: str(p.worker_id), lastHeartbeatAt: str(p.last_heartbeat_at) });
        break;
      }
      case 'job.status_changed': {
        const from = str(p.from);
        const to = str(p.to);
        timeline.status = to;
        const owner = attemptNo !== null ? lanes.get(attemptNo) : undefined;
        if (to === 'cancel_requested') {
          // Written by the API, which doesn't know the attempt: it is the one running now.
          const running = owner ?? lastLane();
          if (running) running.cancelRequestedAt = at;
        }
        if (to === 'retry_wait') {
          if (owner && owner.outcome !== 'lease_expired') {
            owner.outcome = 'retry';
            owner.endedAt = at;
          }
          addGap({ kind: 'retry_wait', at, errorClass: str(p.error_class), retryAt: str(p.retry_at), waitedMs: null });
        }
        if (to === 'succeeded' || to === 'failed' || to === 'cancelled') {
          if (owner && owner.outcome !== 'lease_expired' && (from === 'running' || from === 'cancel_requested')) {
            owner.outcome = to === 'succeeded' ? 'succeeded' : to === 'failed' ? 'failed' : 'cancelled';
            owner.endedAt = at;
          }
          if (to === 'cancelled' && owner && owner.outcome === 'cancelled') {
            owner.cells.forEach((c) => {
              if (c.state === 'pending') c.state = 'not_run';
            });
          }
          timeline.cancelledWhileWaiting = to === 'cancelled' && (from === 'queued' || from === 'retry_wait');
          timeline.final = {
            status: to,
            at,
            errorClass: str(p.error_class),
            beforeStage: str(p.before_stage),
            gaveUpAfter: num(p.gave_up_after_attempts),
            cancelSuperseded: false,
          };
        }
        if (from === 'failed' && to === 'queued') timeline.final = null; // reopened by a manual retry
        break;
      }
      case 'job.cancel_superseded':
        if (timeline.final) timeline.final.cancelSuperseded = true;
        break;
      case 'job.manual_retry': {
        const maxAttempts = num(p.max_attempts);
        const attemptCount = num(p.attempt_count);
        addGap({
          kind: 'manual_retry',
          at,
          count: num(p.manual_retry_count),
          firstAttempt: attemptCount === null ? null : attemptCount + 1,
          maxAttempts,
        });
        break;
      }
      case 'job.requeued':
        addGap({ kind: 'requeued', at });
        break;
    }
  }

  // How long each retry wait really lasted: until the next attempt started.
  timeline.lanes.forEach((owner, index) => {
    const next = timeline.lanes[index + 1];
    owner.after.forEach((gap) => {
      if (gap.kind === 'retry_wait') gap.waitedMs = ms(gap.at, next?.startedAt ?? null);
    });
    owner.cells.forEach((c) => {
      if (c.state === 'done' && c.durationMs !== null) timeline.longestStageMs = Math.max(timeline.longestStageMs, c.durationMs);
    });
  });
  return timeline;
}
