import { Link } from 'react-router';
import { SafeText } from '../../components/SafeText';
import { StatusBadge } from '../../components/Status';
import { ProgressBar, SkeletonRows } from '../../components/ui';
import { isTerminal, type JobListItem } from '../../lib/api/types';
import { durationBetween, formatDateTime, formatDuration, percent } from '../../lib/format';
import { useNow } from '../../lib/hooks';
import { WORKFLOW_LABEL, stageLabel } from '../../lib/labels';

const COLUMNS = 8;

export function JobsTable({
  jobs,
  loading,
  caption,
  stale,
}: {
  jobs: JobListItem[] | undefined;
  loading: boolean;
  caption: string;
  stale?: boolean;
}) {
  const ticking = (jobs ?? []).some((job) => job.started_at && !job.finished_at);
  const now = useNow(ticking);
  return (
    <div className="table-scroll">
      <table className="table jobs-table" data-stale={stale || undefined} aria-busy={loading || undefined}>
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">State</th>
            <th scope="col">File</th>
            <th scope="col">Workflow</th>
            <th scope="col">Stage</th>
            <th scope="col" className="col-progress">
              Progress
            </th>
            <th scope="col" className="num">
              Attempts
            </th>
            <th scope="col">Created</th>
            <th scope="col" className="num">
              Duration
            </th>
          </tr>
        </thead>
        <tbody>
          {loading && !jobs ? (
            <SkeletonRows rows={5} columns={COLUMNS} />
          ) : (
            jobs?.map((job) => (
              <tr key={job.id}>
                <td>
                  <StatusBadge status={job.status} />
                </td>
                <td className="cell-file">
                  <Link to={`/jobs/${job.id}`} className="row-link">
                    <SafeText text={job.upload_filename} />
                    <span className="visually-hidden">, job {job.id}</span>
                  </Link>
                </td>
                <td className="muted">{WORKFLOW_LABEL[job.workflow_type] ?? job.workflow_type}</td>
                <td className="muted">{isTerminal(job.status) ? '—' : stageLabel(job.current_stage)}</td>
                <td className="col-progress">
                  {/* A finished job's bar would only repeat its state; the number still says where it stopped. */}
                  <span className="progress-cell">
                    {!isTerminal(job.status) && (
                      <ProgressBar value={job.progress_percent} label={`Progress of ${job.upload_filename}`} tone={job.status} />
                    )}
                    <span className={isTerminal(job.status) ? 'num muted' : 'num'}>{percent(job.progress_percent)}</span>
                  </span>
                </td>
                <td className="num">
                  {job.attempt_count} of {job.max_attempts}
                </td>
                <td className="muted nowrap">{formatDateTime(job.created_at)}</td>
                <td className="num">{formatDuration(durationBetween(job.started_at, job.finished_at, now))}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
