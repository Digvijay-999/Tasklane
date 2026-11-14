import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { jobDetail, jsonResponse } from '../../test/fixtures';
import { JobActions } from './JobActions';

function renderActions(job = jobDetail(), onChanged = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <JobActions job={job} onChanged={onChanged} />
    </QueryClientProvider>,
  );
  return onChanged;
}

afterEach(() => vi.unstubAllGlobals());

describe('JobActions', () => {
  it('enables Cancel and disables Retry with the server reason for a running job', () => {
    renderActions();
    expect(screen.getByRole('button', { name: 'Cancel job' })).toBeEnabled();
    const retry = screen.getByRole('button', { name: 'Retry job' });
    expect(retry).toBeDisabled();
    expect(retry).toHaveAccessibleDescription('Only failed jobs can be retried.');
  });

  it('follows can_retry / can_cancel, not the status', () => {
    // A failed job the server refuses to retry (input error) stays disabled, with the reason.
    renderActions(
      jobDetail({
        status: 'failed',
        can_cancel: false,
        cancel_blocked_reason: 'already_finished',
        can_retry: false,
        retry_blocked_reason: 'input_error',
      }),
    );
    expect(screen.getByRole('button', { name: 'Retry job' })).toBeDisabled();
    expect(screen.getByText(/failed because of its input file/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Cancel job' })).toBeDisabled();
    expect(screen.getByText('The job has finished, so there is nothing to cancel.')).toBeVisible();
  });

  it('shows the cancel_requested reason while a cancel is pending', () => {
    renderActions(jobDetail({ status: 'cancel_requested', can_cancel: false, cancel_blocked_reason: 'already_cancel_requested' }));
    expect(screen.getByRole('button', { name: 'Cancel job' })).toHaveAccessibleDescription(
      'Cancellation is already requested. The worker stops at the next stage boundary.',
    );
  });

  it('enables Retry when the server allows it, and re-fetches instead of updating optimistically', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(jobDetail({ status: 'queued' })));
    vi.stubGlobal('fetch', fetchMock);
    const onChanged = renderActions(
      jobDetail({ status: 'failed', can_cancel: false, cancel_blocked_reason: 'already_finished', can_retry: true, retry_blocked_reason: null }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Retry job' }));
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/jobs/3f2a9c0e-5b7d-4e1f-8a6b-2c3d4e5f6a7b/retry',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(onChanged).toHaveBeenCalledWith('retry');
  });

  it('shows the server message and code next to the button when retry is refused', async () => {
    vi.stubGlobal('fetch', async () =>
      jsonResponse({ error: { code: 'job_not_retryable', message: 'The job is not failed.', details: {}, request_id: 'r' } }, 409),
    );
    renderActions(jobDetail({ status: 'failed', can_retry: true, retry_blocked_reason: null }));
    await userEvent.click(screen.getByRole('button', { name: 'Retry job' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The job is not failed. job_not_retryable');
  });

  it('asks for confirmation before cancelling', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(jobDetail({ status: 'cancel_requested' })));
    vi.stubGlobal('fetch', fetchMock);
    const onChanged = renderActions();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel job' }));
    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Cancel this job?');
    await userEvent.click(screen.getByRole('button', { name: 'Keep running' }));
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Cancel job' }));
    await screen.findByRole('alertdialog');
    await userEvent.click(screen.getAllByRole('button', { name: 'Cancel job' }).at(-1)!);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/cancel$/), expect.objectContaining({ method: 'POST' }));
    expect(onChanged).toHaveBeenCalledWith('cancel');
  });
});
