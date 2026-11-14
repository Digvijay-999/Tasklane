import { useInfiniteQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { StatusGlyph } from '../../components/Status';
import { ErrorNotice, StaleNote } from '../../components/ui';
import { api, type JobFilters } from '../../lib/api/client';
import { JOB_STATUSES, type JobStatus, type WorkflowType } from '../../lib/api/types';
import { STATUS_LABEL, WORKFLOW_LABEL } from '../../lib/labels';
import { JobsTable } from './JobsTable';

const WORKFLOWS: WorkflowType[] = ['pdf_ingest', 'repo_index'];

/** Filters live in the URL (?workflow=...&status=...&status=...), so a view can be shared or
 * reloaded. Unknown values are ignored rather than sent to the API. */
export function filtersFromParams(params: URLSearchParams): JobFilters {
  return {
    status: params.getAll('status').filter((s): s is JobStatus => (JOB_STATUSES as readonly string[]).includes(s)),
    workflow: params.getAll('workflow').filter((w): w is WorkflowType => (WORKFLOWS as string[]).includes(w)),
  };
}

export function JobsPage() {
  const [params, setParams] = useSearchParams();
  const filters = filtersFromParams(params);
  const filtered = filters.status.length > 0 || filters.workflow.length > 0;

  const jobs = useInfiniteQuery({
    queryKey: ['jobs', 'list', filters],
    queryFn: ({ pageParam }) => api.listJobs(filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    refetchInterval: 5000,
  });
  const items = jobs.data?.pages.flatMap((page) => page.items);

  const update = (key: 'status' | 'workflow', values: string[]) => {
    const next = new URLSearchParams(params);
    next.delete(key);
    values.forEach((value) => next.append(key, value));
    setParams(next, { replace: true });
  };
  const toggleStatus = (status: JobStatus) =>
    update('status', filters.status.includes(status) ? filters.status.filter((s) => s !== status) : [...filters.status, status]);

  return (
    <>
      <div className="page-head">
        <h1>Jobs</h1>
      </div>

      <div className="filters" role="group" aria-label="Filters">
        <fieldset className="segmented">
          <legend>Workflow</legend>
          <label>
            <input type="radio" name="workflow" checked={filters.workflow.length === 0} onChange={() => update('workflow', [])} />
            <span>All</span>
          </label>
          {WORKFLOWS.map((workflow) => (
            <label key={workflow}>
              <input
                type="radio"
                name="workflow"
                checked={filters.workflow.length === 1 && filters.workflow[0] === workflow}
                onChange={() => update('workflow', [workflow])}
              />
              <span>{WORKFLOW_LABEL[workflow]}</span>
            </label>
          ))}
        </fieldset>
        <fieldset className="chips">
          <legend>State</legend>
          {JOB_STATUSES.map((status) => (
            <label key={status} data-status={status}>
              <input type="checkbox" checked={filters.status.includes(status)} onChange={() => toggleStatus(status)} />
              <span>
                <StatusGlyph status={status} size={12} />
                {STATUS_LABEL[status]}
              </span>
            </label>
          ))}
        </fieldset>
        {filtered && (
          <button type="button" className="button" data-variant="ghost" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
            Clear filters
          </button>
        )}
      </div>

      {jobs.isError && !jobs.data && <ErrorNotice error={jobs.error} />}
      <StaleNote error={jobs.isError && jobs.error} updatedAt={jobs.dataUpdatedAt} />

      {items && items.length === 0 ? (
        <div className="empty">
          {filtered ? (
            <>
              <p>No jobs match these filters.</p>
              <button type="button" className="button" data-variant="secondary" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
                Clear filters
              </button>
            </>
          ) : (
            <>
              <p>No jobs yet. Upload a PDF or a repository ZIP to start one.</p>
              <Link to="/jobs/new" className="button" data-variant="primary">
                New job
              </Link>
            </>
          )}
        </div>
      ) : (
        <JobsTable
          jobs={items}
          loading={jobs.isPending}
          caption={filtered ? 'Jobs matching the filters, newest first' : 'All jobs, newest first'}
          stale={jobs.isError && !!jobs.data}
        />
      )}

      {jobs.hasNextPage && (
        <div className="load-more">
          <button
            type="button"
            className="button"
            data-variant="secondary"
            onClick={() => void jobs.fetchNextPage()}
            disabled={jobs.isFetchingNextPage}
          >
            {jobs.isFetchingNextPage ? 'Loading more jobs…' : 'Load more jobs'}
          </button>
        </div>
      )}
    </>
  );
}
