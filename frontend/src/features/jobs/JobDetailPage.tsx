import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { SafeText } from '../../components/SafeText';
import { StatusBadge } from '../../components/Status';
import { ErrorNotice, ProgressBar, Skeleton, StaleNote } from '../../components/ui';
import { ApiError, api } from '../../lib/api/client';
import { isTerminal, type Artifact, type JobDetail, type JobEvent } from '../../lib/api/types';
import { durationBetween, formatBytes, formatDateTime, formatDuration, formatTime, percent } from '../../lib/format';
import { useNow, useWorkflowMeta } from '../../lib/hooks';
import { OUTCOME_LABEL, STATUS_LABEL, TRIGGER_LABEL, WORKFLOW_LABEL } from '../../lib/labels';
import type { StreamMode } from '../../lib/live/jobEventStream';
import { useJobEvents } from '../../lib/live/useJobEvents';
import { EventLog } from './EventLog';
import { JobActions } from './JobActions';
import { ManifestViewer } from './Manifest';
import { Timeline } from './Timeline';
import { buildTimeline } from './timelineModel';

const SUMMARY_PREVIEW_BYTES = 256 * 1024;

export function JobDetailPage() {
  const { jobId = '' } = useParams();
  // A new key per job: a fresh event stream and no state carried over from another job.
  return <JobDetailView key={jobId} jobId={jobId} />;
}

const LIVE_TEXT: Record<StreamMode, string> = {
  loading: 'Loading the event history…',
  connecting: 'Connecting to live updates…',
  live: 'Live',
  reconnecting: 'Live updates paused, reconnecting…',
  polling: "The server's live-stream limit is reached. Checking for new events every 2 s instead.",
  ended: 'Job finished. Live updates stopped.',
  not_found: 'This job no longer exists.',
  error: "The event history couldn't be loaded.",
};

function LiveStatus({ mode, reconnects, missing, onRetry }: { mode: StreamMode; reconnects: number; missing: number; onRetry: () => void }) {
  return (
    <div className="live" data-mode={mode} role="status">
      <span className="live-dot" aria-hidden="true" />
      <span>
        {LIVE_TEXT[mode]}
        {mode === 'reconnecting' && reconnects > 1 && ` (attempt ${reconnects})`}
        {missing > 0 && ` ${missing} events are missing from the stored history.`}
      </span>
      {mode === 'error' && (
        <button type="button" className="button" data-variant="ghost" onClick={onRetry}>
          Load events again
        </button>
      )}
    </div>
  );
}

function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="copy-id">
      <code className="mono">{id}</code>
      <button
        type="button"
        className="button"
        data-variant="ghost"
        data-size="small"
        onClick={() =>
          void navigator.clipboard?.writeText(id).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
          })
        }
      >
        {copied ? 'Copied' : 'Copy id'}
      </button>
    </span>
  );
}

function ErrorBlock({ job }: { job: JobDetail }) {
  if (job.status !== 'failed' || !job.error_summary) return null;
  return (
    <section className="error-block" aria-labelledby="error-heading">
      <h2 id="error-heading">Why it failed</h2>
      <p>
        <code className="mono">{job.error_class}</code>
      </p>
      <p className="error-message">{job.error_summary}</p>
      <p className="muted">
        {job.can_retry
          ? 'This error may not happen again. Retry the job to run it from the last verified checkpoint.'
          : job.retry_blocked_reason === 'input_error'
            ? 'The problem is in the file itself, so a retry would fail the same way. Fix or replace the file and create a new job.'
            : 'This job cannot be retried. Create a new job to try again.'}
      </p>
      {!job.can_retry && (
        <Link to="/jobs/new" className="button" data-variant="secondary">
          New job
        </Link>
      )}
    </section>
  );
}

function SummaryPreview({ artifact }: { artifact: Artifact }) {
  const preview = useQuery({
    queryKey: ['artifact', artifact.id, artifact.sha256],
    queryFn: () => api.artifactText(artifact.download_url, SUMMARY_PREVIEW_BYTES),
    staleTime: Infinity,
  });
  if (preview.isPending) return <Skeleton height={160} />;
  if (preview.isError) return <ErrorNotice error={preview.error} />;
  return (
    <>
      {/* Plain preformatted text: no Markdown rendering, no HTML, no links (DESIGN.md §7). */}
      <pre className="summary-preview" tabIndex={0} aria-label="Summary preview">
        <SafeText text={preview.data.text} multiline />
      </pre>
      {preview.data.truncated && (
        <p className="muted">Showing the first {formatBytes(SUMMARY_PREVIEW_BYTES)}. Download the file for the rest.</p>
      )}
    </>
  );
}

const DOWNLOAD_LABEL: Record<string, string> = {
  summary: 'Download summary',
  manifest: 'Download manifest',
  chunks: 'Download chunks',
};

function Outputs({ job }: { job: JobDetail }) {
  if (job.artifacts.length === 0) {
    return (
      <p className="muted">
        {job.status === 'succeeded'
          ? 'This job has no recorded outputs.'
          : 'Outputs appear here when the job succeeds. They are recorded together with the success, never for a failed or unfinished job.'}
      </p>
    );
  }
  const summary = job.artifacts.find((a) => a.artifact_type === 'summary');
  return (
    <>
      <table className="table table-compact">
        <caption className="visually-hidden">Output files</caption>
        <thead>
          <tr>
            <th scope="col">File</th>
            <th scope="col" className="num">
              Size
            </th>
            <th scope="col">SHA-256</th>
            <th scope="col">
              <span className="visually-hidden">Download</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {job.artifacts.map((artifact) => (
            <tr key={artifact.id}>
              <td className="mono">
                <SafeText text={artifact.filename} />
              </td>
              <td className="num">{formatBytes(artifact.size_bytes)}</td>
              <td className="mono hash" title={artifact.sha256}>
                {artifact.sha256.slice(0, 12)}
              </td>
              <td className="cell-action">
                <a href={artifact.download_url} download className="button" data-variant="secondary" data-size="small">
                  {DOWNLOAD_LABEL[artifact.artifact_type] ?? 'Download'}
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {summary && (
        <div className="stack">
          <h3>Summary preview</h3>
          <SummaryPreview artifact={summary} />
        </div>
      )}
    </>
  );
}

function Attempts({ job, now }: { job: JobDetail; now: number }) {
  if (job.attempts.length === 0) return <p className="muted">No attempt yet: the job is waiting for a worker.</p>;
  return (
    <div className="table-scroll">
      <table className="table table-compact">
        <caption className="visually-hidden">Attempts</caption>
        <thead>
          <tr>
            <th scope="col" className="num">
              Attempt
            </th>
            <th scope="col">Trigger</th>
            <th scope="col">Worker</th>
            <th scope="col">Started</th>
            <th scope="col" className="num">
              Duration
            </th>
            <th scope="col">Outcome</th>
            <th scope="col">Error class</th>
          </tr>
        </thead>
        <tbody>
          {job.attempts.map((attempt) => (
            <tr key={attempt.attempt_no}>
              <td className="num">{attempt.attempt_no}</td>
              <td>{TRIGGER_LABEL[attempt.trigger] ?? attempt.trigger}</td>
              <td className="mono truncate" title={attempt.worker_id}>
                {attempt.worker_id}
              </td>
              <td className="nowrap">{formatTime(attempt.started_at)}</td>
              <td className="num">{formatDuration(durationBetween(attempt.started_at, attempt.ended_at, now))}</td>
              <td>
                {attempt.outcome ? (
                  <span className="outcome" data-outcome={attempt.outcome}>
                    {OUTCOME_LABEL[attempt.outcome] ?? attempt.outcome}
                  </span>
                ) : (
                  <span className="muted">In progress</span>
                )}
              </td>
              <td className="mono">{attempt.error_class ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobDetailView({ jobId }: { jobId: string }) {
  const queryClient = useQueryClient();
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.job(jobId),
    // Also catches what produces no event: an expiring lease, a dead API.
    refetchInterval: (query) => (query.state.data && isTerminal(query.state.data.status) ? false : 5000),
  });
  const workflow = useWorkflowMeta(job.data?.workflow_type);

  // Events drive the timeline directly; the job snapshot (status, actions, artifacts) is
  // re-read after them, at most about once a second.
  const pending = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const refreshJob = useCallback(
    (delay: number) => {
      if (pending.current !== undefined) return;
      pending.current = setTimeout(() => {
        pending.current = undefined;
        void queryClient.invalidateQueries({ queryKey: ['job', jobId] });
      }, delay);
    },
    [queryClient, jobId],
  );
  useEffect(() => () => clearTimeout(pending.current), []);
  const onNewEvents = useCallback(
    (events: JobEvent[]) => refreshJob(events.some((e) => e.event_type !== 'stage.progress') ? 150 : 1000),
    [refreshJob],
  );
  const stream = useJobEvents(jobId, onNewEvents);
  useEffect(() => {
    if (stream.mode === 'ended') refreshJob(0);
  }, [stream.mode, refreshJob]);

  const stageNames = useMemo(() => workflow?.stages.map((s) => s.name) ?? [], [workflow]);
  const timeline = useMemo(() => buildTimeline(stream.events, stageNames), [stream.events, stageNames]);
  const data = job.data;
  const now = useNow(!!data && !isTerminal(data.status));

  if (job.isPending) {
    return (
      <div aria-busy="true" className="stack">
        <Skeleton width={180} height={14} />
        <Skeleton width={320} height={28} />
        <Skeleton height={64} />
        <Skeleton height={220} />
      </div>
    );
  }
  if (!data) {
    const notFound = job.error instanceof ApiError && job.error.code === 'job_not_found';
    return (
      <>
        <h1>{notFound ? 'Job not found' : 'Job'}</h1>
        {notFound ? <p className="muted">No job exists with this id. It may have been deleted.</p> : <ErrorNotice error={job.error} />}
        <p>
          <Link to="/jobs">Back to jobs</Link>
        </p>
      </>
    );
  }

  const onChanged = (action: 'cancel' | 'retry') => {
    void queryClient.invalidateQueries({ queryKey: ['job', jobId] });
    void queryClient.invalidateQueries({ queryKey: ['jobs'] });
    void queryClient.invalidateQueries({ queryKey: ['overview'] });
    if (action === 'retry') stream.restart(); // the job reopened: follow its new events
  };

  return (
    <>
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <Link to="/jobs">Jobs</Link>
        <span aria-hidden="true">/</span>
        <SafeText text={data.upload.original_filename} />
      </nav>
      <div className="page-head job-head">
        <h1>
          <SafeText text={data.upload.original_filename} />
        </h1>
        <JobActions job={data} onChanged={onChanged} />
      </div>
      {/* Announces status changes to screen readers (not every progress tick). */}
      <p className="visually-hidden" aria-live="polite">
        Job status: {STATUS_LABEL[data.status]}
      </p>
      <StaleNote error={job.isError && job.error} updatedAt={job.dataUpdatedAt} />

      <dl className="facts facts-inline job-facts" data-stale={job.isError || undefined}>
        <div>
          <dt>State</dt>
          <dd>
            <StatusBadge status={data.status} />
          </dd>
        </div>
        <div>
          <dt>Progress</dt>
          <dd className="progress-cell">
            {!isTerminal(data.status) && <ProgressBar value={data.progress_percent} label="Job progress" tone={data.status} />}
            <span className="num">{percent(data.progress_percent)}</span>
          </dd>
        </div>
        <div>
          <dt>Workflow</dt>
          <dd>{WORKFLOW_LABEL[data.workflow_type] ?? data.workflow_type}</dd>
        </div>
        <div>
          <dt>Attempts</dt>
          <dd className="num">
            {data.attempt_count} of {data.max_attempts}
            {data.manual_retry_count > 0 && <span className="muted"> ({data.manual_retry_count} manual)</span>}
          </dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd className="num">{formatDateTime(data.created_at)}</dd>
        </div>
        <div>
          <dt>{data.finished_at ? 'Ran for' : 'Running for'}</dt>
          <dd className="num">{formatDuration(durationBetween(data.started_at, data.finished_at, now))}</dd>
        </div>
        <div className="fact-wide">
          <dt>Job id</dt>
          <dd>
            <CopyId id={data.id} />
          </dd>
        </div>
      </dl>

      {data.lease?.expired && (
        <div className="notice" data-tone="retry_wait" role="status">
          <p>
            Worker not responding: attempt {data.lease.attempt_no} stopped renewing its lease at {formatTime(data.lease.lease_expires_at)}.
            Recovery will retry the job or mark it failed within a few seconds.
          </p>
        </div>
      )}
      {data.status === 'retry_wait' && data.retry_at && (
        <p className="muted">Next attempt at {formatTime(data.retry_at)}.</p>
      )}

      <section className="section" aria-labelledby="timeline-heading">
        <div className="section-head">
          <h2 id="timeline-heading">Timeline</h2>
          <LiveStatus mode={stream.mode} reconnects={stream.reconnects} missing={stream.missing} onRetry={stream.restart} />
        </div>
        {stream.mode === 'loading' && stream.events.length === 0 ? (
          <Skeleton height={120} />
        ) : (
          <Timeline model={timeline} stages={stageNames} errorSummary={data.error_summary} queuedSince={data.updated_at} />
        )}
      </section>

      <ErrorBlock job={data} />

      <section className="section" aria-labelledby="attempts-heading">
        <h2 id="attempts-heading">Attempts</h2>
        <Attempts job={data} now={now} />
      </section>

      <section className="section" aria-labelledby="outputs-heading">
        <h2 id="outputs-heading">Outputs</h2>
        <Outputs job={data} />
      </section>

      {data.workflow_type === 'repo_index' && (
        <section className="section" aria-labelledby="manifest-heading">
          <h2 id="manifest-heading">Manifest</h2>
          {data.artifacts.find((a) => a.artifact_type === 'manifest') ? (
            <ManifestViewer artifact={data.artifacts.find((a) => a.artifact_type === 'manifest')!} />
          ) : (
            <p className="muted">The manifest (included files, skipped files and why) is available once the job succeeds.</p>
          )}
        </section>
      )}

      <section className="section" aria-labelledby="events-heading">
        <h2 id="events-heading">Event history</h2>
        <EventLog events={stream.events} />
      </section>
    </>
  );
}
