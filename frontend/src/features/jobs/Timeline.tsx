import { StatusGlyph } from '../../components/Status';
import { durationBetween, formatDuration, formatTime, percent } from '../../lib/format';
import { useNow } from '../../lib/hooks';
import { STATUS_LABEL, TRIGGER_LABEL, stageLabel } from '../../lib/labels';
import type { JobStatus } from '../../lib/api/types';
import type { Gap, Lane, StageCell, Timeline as TimelineModel } from './timelineModel';

function CellGlyph({ state }: { state: StageCell['state'] }) {
  const common = { width: 12, height: 12, viewBox: '0 0 12 12', 'aria-hidden': true, focusable: false } as const;
  switch (state) {
    case 'done':
      return (
        <svg {...common}>
          <path d="M2.2 6.3l2.4 2.4 5-5.2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case 'running':
      return <StatusGlyph status="running" size={12} />;
    case 'reused':
      return (
        <svg {...common}>
          <path d="M2 8.5a4 4 0 0 1 7.2-3.2" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <path d="M10.4 2.6v3.5H6.9z" fill="currentColor" />
        </svg>
      );
    case 'failed':
      return (
        <svg {...common}>
          <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
        </svg>
      );
    case 'interrupted':
      return (
        <svg {...common}>
          <path d="M6 1.5a4.5 4.5 0 0 1 4.5 4.5M10.5 6A4.5 4.5 0 0 1 6 10.5M3.4 9.6A4.5 4.5 0 0 1 2.4 3.3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      );
    case 'not_run':
      return (
        <svg {...common}>
          <rect x="3" y="3" width="6" height="6" rx="1" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="1.6" fill="currentColor" />
        </svg>
      );
  }
}

function describeCell(cell: StageCell, elapsed: number | null): string {
  const name = stageLabel(cell.stage);
  switch (cell.state) {
    case 'done':
      return `${name}: done in ${formatDuration(cell.durationMs)}`;
    case 'running':
      return `${name}: running, ${percent(cell.percent ?? 0)}, ${formatDuration(elapsed)} so far`;
    case 'reused':
      return `${name}: reused the checkpoint from an earlier attempt, not run again`;
    case 'failed':
      return `${name}: failed after ${formatDuration(cell.durationMs)}, ${cell.errorClass ?? 'unknown error'}${
        cell.retryable === null ? '' : cell.retryable ? ', transient' : ', permanent'
      }`;
    case 'interrupted':
      return `${name}: interrupted, the worker stopped responding`;
    case 'not_run':
      return `${name}: not run, the job was cancelled`;
    default:
      return `${name}: not reached`;
  }
}

function Cell({ cell, longestMs, now }: { cell: StageCell; longestMs: number; now: number }) {
  const elapsed = cell.state === 'running' ? durationBetween(cell.startedAt, null, now) : null;
  const label = describeCell(cell, elapsed);
  return (
    <td className="tl-cell" data-state={cell.state} aria-label={label}>
      {/* `key` restarts the fade-in only when the state changes, not on every progress tick. */}
      <div className="tl-cell-body" key={cell.state} aria-hidden="true">
        <span className="tl-cell-line">
          <CellGlyph state={cell.state} />
          {cell.state === 'done' && <span className="num">{formatDuration(cell.durationMs)}</span>}
          {cell.state === 'running' && (
            <span className="num">
              {percent(cell.percent ?? 0)} <span className="muted">{formatDuration(elapsed)}</span>
            </span>
          )}
          {cell.state === 'reused' && <span>Reused</span>}
          {cell.state === 'failed' && <span className="num">{formatDuration(cell.durationMs)}</span>}
          {cell.state === 'interrupted' && <span>Worker lost</span>}
          {cell.state === 'not_run' && <span>Not run</span>}
        </span>
        {cell.state === 'done' && (
          <span className="tl-bar" style={{ transform: `scaleX(${longestMs ? Math.max(0.02, (cell.durationMs ?? 0) / longestMs) : 0})` }} />
        )}
        {cell.state === 'running' && (
          <span className="tl-progress">
            <span className="progress-fill" style={{ transform: `scaleX(${(cell.percent ?? 0) / 100})` }} />
          </span>
        )}
        {cell.state === 'failed' && (
          <span className="tl-detail">
            <code className="mono">{cell.errorClass ?? 'error'}</code>
            {cell.retryable !== null && <span>{cell.retryable ? 'transient' : 'permanent'}</span>}
          </span>
        )}
      </div>
    </td>
  );
}

function gapSentence(gap: Gap, now: number): string {
  switch (gap.kind) {
    case 'retry_wait': {
      const reason = gap.errorClass ? ` after ${gap.errorClass}, a transient error` : '';
      if (gap.waitedMs !== null) return `Waited ${formatDuration(gap.waitedMs)}, then retried automatically${reason}.`;
      const left = gap.retryAt ? Date.parse(gap.retryAt) - now : null;
      return left !== null && left > 0
        ? `Retrying automatically in ${formatDuration(left)}${reason}.`
        : `Retry is due${reason}; waiting for a worker.`;
    }
    case 'lease_expired':
      return `The worker stopped renewing its lease${
        gap.lastHeartbeatAt ? ` (last heartbeat ${formatTime(gap.lastHeartbeatAt)})` : ''
      }. Recovery took the job back at ${formatTime(gap.at)}.`;
    case 'manual_retry':
      return `Retried by hand at ${formatTime(gap.at)}${gap.count ? ` (manual retry ${gap.count})` : ''}.${
        gap.firstAttempt && gap.maxAttempts ? ` New budget: attempts ${gap.firstAttempt} to ${gap.maxAttempts}.` : ''
      }`;
    case 'requeued':
      return `The queue message was lost; recovery sent a new one at ${formatTime(gap.at)}.`;
  }
}

function GapRow({ gap, columns, now }: { gap: Gap; columns: number; now: number }) {
  return (
    <tr className="tl-gap" data-kind={gap.kind}>
      <td colSpan={columns}>
        <span className="tl-gap-text">{gapSentence(gap, now)}</span>
      </td>
    </tr>
  );
}

function laneDuration(lane: Lane, now: number): number | null {
  return durationBetween(lane.startedAt, lane.endedAt, now);
}

function LaneRows({ lane, model, columns, now }: { lane: Lane; model: TimelineModel; columns: number; now: number }) {
  return (
    <>
      <tr className="tl-lane" data-outcome={lane.outcome ?? 'running'}>
        <th scope="row" className="tl-lane-head">
          <span className="tl-lane-title">Attempt {lane.attemptNo}</span>
          <span className="muted">{TRIGGER_LABEL[lane.trigger ?? ''] ?? lane.trigger ?? '—'}</span>
          <span className="muted num">
            {lane.endedAt ? 'Ran' : 'Running for'} {formatDuration(laneDuration(lane, now))}
          </span>
          {lane.workerId && (
            <span className="mono tl-worker" title={lane.workerId}>
              {lane.workerId}
            </span>
          )}
        </th>
        {lane.cells.map((cell) => (
          <Cell key={cell.stage} cell={cell} longestMs={model.longestStageMs} now={now} />
        ))}
      </tr>
      {lane.cancelRequestedAt && lane.outcome === null && (
        <tr className="tl-gap" data-kind="cancel_requested">
          <td colSpan={columns}>
            <span className="tl-gap-text">
              Cancel requested at {formatTime(lane.cancelRequestedAt)}. The worker stops at the next stage boundary.
            </span>
          </td>
        </tr>
      )}
      {lane.after.map((gap, index) => (
        <GapRow key={index} gap={gap} columns={columns} now={now} />
      ))}
    </>
  );
}

function finalSentence(model: TimelineModel, errorSummary: string | null): string {
  const final = model.final!;
  const total = formatDuration(durationBetween(model.lanes[0]?.startedAt ?? model.createdAt, final.at));
  if (final.status === 'succeeded') {
    return `Succeeded in ${total}${final.cancelSuperseded ? '. Cancellation was requested, but the last stage finished first' : ''}.`;
  }
  if (final.status === 'failed') {
    // The message itself is in "Why it failed" right below; this line says where and what.
    const failedCell = model.lanes[model.lanes.length - 1]?.cells.find((c) => c.state === 'failed' || c.state === 'interrupted');
    const where = failedCell ? ` in ${stageLabel(failedCell.stage)}` : '';
    const what = final.errorClass ?? errorSummary ?? 'unknown error';
    return `Failed${where}${final.gaveUpAfter ? ` after ${final.gaveUpAfter} attempts` : ''}: ${what}${
      final.cancelSuperseded ? '. The failure came before the requested cancellation.' : '.'
    }`;
  }
  if (model.cancelledWhileWaiting) return 'Cancelled before a worker started it again.';
  return final.beforeStage ? `Cancelled before ${stageLabel(final.beforeStage)}.` : 'Cancelled.';
}

function activeSentence(model: TimelineModel, status: JobStatus, now: number): string {
  const lane = model.lanes[model.lanes.length - 1];
  const running = lane?.cells.find((c) => c.state === 'running');
  if (status === 'running' && lane) {
    return `Running attempt ${lane.attemptNo}${running ? `, now in ${stageLabel(running.stage)}` : ''}.`;
  }
  if (status === 'cancel_requested') {
    return `Cancelling: the worker stops after ${running ? stageLabel(running.stage) : 'the current stage'}.`;
  }
  if (status === 'retry_wait') {
    const gap = lane?.after.findLast((g) => g.kind === 'retry_wait');
    const left = gap?.kind === 'retry_wait' && gap.retryAt ? Date.parse(gap.retryAt) - now : null;
    return left !== null && left > 0 ? `Waiting to retry, next attempt in ${formatDuration(left)}.` : 'Waiting to retry; the next attempt is due.';
  }
  return `${STATUS_LABEL[status] ?? status}, waiting for a worker.`;
}

export function Timeline({
  model,
  stages,
  errorSummary,
  queuedSince,
}: {
  model: TimelineModel;
  stages: readonly string[];
  errorSummary: string | null;
  queuedSince: string | null;
}) {
  const active = model.final === null;
  const now = useNow(active);
  const columns = stages.length + 1;
  const status = (model.status ?? 'queued') as JobStatus;
  return (
    <div className="timeline-wrap">
      <table className="timeline">
        <caption className="visually-hidden">Attempts (rows) and stages (columns) of this job</caption>
        <colgroup>
          <col className="tl-col-head" />
          {stages.map((stage) => (
            <col key={stage} />
          ))}
        </colgroup>
        <thead>
          <tr>
            <td />
            {stages.map((stage) => (
              <th key={stage} scope="col" title={stage}>
                {stageLabel(stage)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {model.before.map((gap, index) => (
            <GapRow key={`before-${index}`} gap={gap} columns={columns} now={now} />
          ))}
          {model.lanes.length === 0 && !model.final && (
            <tr className="tl-gap" data-kind="queued">
              <td colSpan={columns}>
                <span className="tl-gap-text">
                  Queued, waiting for a worker{queuedSince ? ` for ${formatDuration(durationBetween(queuedSince, null, now))}` : ''}.
                </span>
              </td>
            </tr>
          )}
          {model.lanes.map((lane) => (
            <LaneRows key={lane.attemptNo} lane={lane} model={model} columns={columns} now={now} />
          ))}
        </tbody>
      </table>
      {model.final ? (
        <p className="tl-final" data-status={model.final.status}>
          <StatusGlyph status={model.final.status} size={16} />
          <span>{finalSentence(model, errorSummary)}</span>
        </p>
      ) : (
        <p className="tl-final" data-status={status}>
          <StatusGlyph status={status} size={16} />
          <span>{activeSentence(model, status, now)}</span>
        </p>
      )}
    </div>
  );
}
