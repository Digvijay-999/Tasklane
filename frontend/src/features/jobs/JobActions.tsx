import { useMutation } from '@tanstack/react-query';
import { useId } from 'react';
import { ConfirmDialog, ErrorNotice } from '../../components/ui';
import { api } from '../../lib/api/client';
import type { JobDetail } from '../../lib/api/types';
import { CANCEL_BLOCKED, RETRY_BLOCKED } from '../../lib/labels';

/**
 * Cancel and Retry, enabled ONLY by the server's can_cancel / can_retry, with the server's reason
 * shown when not allowed (never derived from `status` here). After either request the caller
 * re-fetches the job; nothing is updated optimistically.
 */
export function JobActions({ job, onChanged }: { job: JobDetail; onChanged: (action: 'cancel' | 'retry') => void }) {
  const cancelReasonId = useId();
  const retryReasonId = useId();
  const cancel = useMutation({ mutationFn: () => api.cancel(job.id), onSettled: () => onChanged('cancel') });
  const retry = useMutation({ mutationFn: () => api.retry(job.id), onSettled: () => onChanged('retry') });

  const cancelReason = !job.can_cancel && job.cancel_blocked_reason ? CANCEL_BLOCKED[job.cancel_blocked_reason] : null;
  const retryReason = !job.can_retry && job.retry_blocked_reason ? RETRY_BLOCKED[job.retry_blocked_reason] : null;
  const running = job.status === 'running';

  return (
    <div className="actions">
      <div className="action-buttons">
        <ConfirmDialog
          trigger="Cancel job"
          disabled={!job.can_cancel || cancel.isPending}
          describedBy={cancelReason ? cancelReasonId : undefined}
          title="Cancel this job?"
          description={
            running
              ? 'The worker finishes the stage it is in, then stops before the next one. A cancelled job cannot be retried.'
              : 'The job will not run. A cancelled job cannot be retried.'
          }
          cancelLabel={running ? 'Keep running' : 'Keep job'}
          confirmLabel="Cancel job"
          onConfirm={() => cancel.mutate()}
        />
        <button
          type="button"
          className="button"
          data-variant={job.can_retry ? 'primary' : 'secondary'}
          disabled={!job.can_retry || retry.isPending}
          aria-describedby={retryReason ? retryReasonId : undefined}
          onClick={() => retry.mutate()}
        >
          {retry.isPending ? 'Retrying…' : 'Retry job'}
        </button>
      </div>
      {(cancelReason || retryReason) && (
        // Each reason names its action, so the list reads unambiguously next to the two buttons.
        <ul className="action-reasons">
          {cancelReason && (
            <li id={cancelReasonId}>
              {cancelReason}
            </li>
          )}
          {retryReason && (
            <li id={retryReasonId}>
              {retryReason}
            </li>
          )}
        </ul>
      )}
      {cancel.isError && <ErrorNotice error={cancel.error} />}
      {retry.isError && <ErrorNotice error={retry.error} />}
    </div>
  );
}
