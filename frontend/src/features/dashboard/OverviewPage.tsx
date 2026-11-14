import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { StatusGlyph } from '../../components/Status';
import { ErrorNotice, Skeleton, StaleNote } from '../../components/ui';
import { ApiError, api } from '../../lib/api/client';
import { JOB_STATUSES, type Overview } from '../../lib/api/types';
import { formatDuration, shortId } from '../../lib/format';
import { STATUS_LABEL } from '../../lib/labels';
import { JobsTable } from '../jobs/JobsTable';

const REFRESH_MS = 3000;

function Counts({ overview }: { overview: Overview | undefined }) {
  return (
    <ul className="counts" aria-label="Jobs by state">
      {JOB_STATUSES.map((status) => (
        <li key={status} data-status={status}>
          <Link to={`/jobs?status=${status}`} className="count-link">
            <span className="count-label">
              <StatusGlyph status={status} />
              {STATUS_LABEL[status]}
            </span>
            <span className="count-value" data-zero={overview?.counts[status] === 0 || undefined}>
              {overview ? overview.counts[status] : <Skeleton width={32} height={28} />}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function Check({ ok, okText, badText, stale }: { ok: boolean; okText: string; badText: string; stale?: boolean }) {
  // A stale value never wears the green/red of a current check.
  if (stale) return <span className="muted">Last known: {ok ? okText : badText}</span>;
  return (
    <span className="status" data-status={ok ? 'succeeded' : 'failed'}>
      <StatusGlyph status={ok ? 'succeeded' : 'failed'} />
      <span>{ok ? okText : badText}</span>
    </span>
  );
}

function Queue({ overview, error }: { overview: Overview | undefined; error: unknown }) {
  if (!overview) {
    return (
      <dl className="facts facts-2" aria-busy="true">
        {['Database', 'Redis broker', 'Dispatcher', 'Oldest queued job', 'Messages waiting', 'Scheduled retries'].map((label) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{error ? '—' : <Skeleton width={120} />}</dd>
          </div>
        ))}
      </dl>
    );
  }
  const q = overview.queue;
  const stale = !!error;
  return (
    <dl className="facts facts-2">
      <div>
        <dt>Database</dt>
        <dd>
          {error instanceof ApiError && error.unreachable ? (
            <span className="muted">Unknown while the API is unreachable</span>
          ) : (
            <Check ok={!error} okText="Reachable" badText="Unreachable" />
          )}
        </dd>
      </div>
      <div>
        <dt>Oldest queued job</dt>
        <dd>
          {overview.oldest_queued ? (
            <>
              <span className="num">{formatDuration(overview.oldest_queued.queued_seconds * 1000)}</span>{' '}
              <Link to={`/jobs/${overview.oldest_queued.job_id}`} className="mono">
                {shortId(overview.oldest_queued.job_id)}
              </Link>
            </>
          ) : (
            <span className="muted">No job is waiting</span>
          )}
        </dd>
      </div>
      <div>
        <dt>Redis broker</dt>
        <dd>
          <Check ok={q.broker === 'ok'} okText="Reachable" badText="Unreachable: jobs stay queued until it is back" stale={stale} />
        </dd>
      </div>
      <div>
        <dt>Messages waiting to be published</dt>
        <dd className="num">
          {q.pending_messages}
          {q.oldest_pending_seconds !== null && q.pending_messages > 0 && (
            <span className="muted"> (oldest {formatDuration(q.oldest_pending_seconds * 1000)})</span>
          )}
        </dd>
      </div>
      <div>
        <dt>Dispatcher</dt>
        <dd>
          {q.failing_messages === 0 ? (
            <Check ok okText="Publishing normally" badText="" stale={stale} />
          ) : (
            <span className="status" data-status="failed">
              <StatusGlyph status="failed" />
              <span>
                {q.failing_messages} {q.failing_messages === 1 ? 'message' : 'messages'} failing to publish after up to{' '}
                {q.max_dispatch_attempts} tries
                {q.last_dispatch_error && (
                  <>
                    {' '}
                    (<code className="mono">{q.last_dispatch_error}</code>)
                  </>
                )}
              </span>
            </span>
          )}
        </dd>
      </div>
      <div>
        <dt>Scheduled retries</dt>
        <dd className="num">{q.scheduled_messages}</dd>
      </div>
    </dl>
  );
}

export function OverviewPage() {
  const overview = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: REFRESH_MS });
  const recent = useQuery({
    queryKey: ['jobs', 'recent'],
    queryFn: () => api.listJobs({ status: [], workflow: [] }, null, 8),
    refetchInterval: REFRESH_MS,
  });
  const empty = overview.data?.total === 0;

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
      </div>

      {overview.isError && !overview.data && (
        <ErrorNotice error={overview.error} action={<p className="muted">The page keeps retrying every few seconds.</p>} />
      )}

      <section aria-labelledby="counts-heading" className="section">
        <div className="section-head">
          <h2 id="counts-heading">Jobs by state</h2>
          {overview.data && (
            <span className="muted num">
              {overview.data.total} {overview.data.total === 1 ? 'job' : 'jobs'} in total
            </span>
          )}
        </div>
        <StaleNote error={overview.isError && overview.error} updatedAt={overview.dataUpdatedAt} />
        <div data-stale={(overview.isError && !!overview.data) || undefined}>
          <Counts overview={overview.data} />
        </div>
      </section>

      <section aria-labelledby="queue-heading" className="section">
        <div className="section-head">
          <h2 id="queue-heading">Queue</h2>
        </div>
        <div data-stale={(overview.isError && !!overview.data) || undefined}>
          <Queue overview={overview.data} error={overview.isError ? overview.error : null} />
        </div>
      </section>

      <section aria-labelledby="recent-heading" className="section">
        <div className="section-head">
          <h2 id="recent-heading">Recent jobs</h2>
          {!empty && <Link to="/jobs">View all jobs</Link>}
        </div>
        <StaleNote error={recent.isError && recent.error} updatedAt={recent.dataUpdatedAt} />
        {empty ? (
          <div className="empty">
            <p>No jobs yet. Upload a PDF or a repository ZIP to start one; it will appear here.</p>
            <Link to="/jobs/new" className="button" data-variant="primary">
              New job
            </Link>
          </div>
        ) : (
          <JobsTable
            jobs={recent.data?.items}
            loading={recent.isPending}
            caption="The 8 most recent jobs"
            stale={recent.isError && !!recent.data}
          />
        )}
      </section>
    </>
  );
}
