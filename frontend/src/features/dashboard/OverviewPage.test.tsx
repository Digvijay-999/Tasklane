import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Overview } from '../../lib/api/types';
import { jsonResponse } from '../../test/fixtures';
import { renderPage } from '../../test/render';
import { OverviewPage } from './OverviewPage';

function overview(workers: Overview['workers']): Overview {
  return {
    counts: { queued: 2, running: 0, retry_wait: 0, cancel_requested: 0, succeeded: 1, failed: 0, cancelled: 0 },
    total: 3,
    oldest_queued: null,
    queue: {
      broker: 'ok',
      pending_messages: 0,
      scheduled_messages: 0,
      failing_messages: 0,
      max_dispatch_attempts: 0,
      last_dispatch_error: null,
      oldest_pending_seconds: null,
    },
    workers,
    database_time: '2026-11-17T12:00:00Z',
  };
}

function stub(body: Overview) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      String(url).includes('/v1/overview') ? jsonResponse(body) : jsonResponse({ items: [], next_cursor: null }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('OverviewPage workers', () => {
  it('says "No worker is running" when no worker process reports', async () => {
    stub(overview({ alive: 0, last_seen_seconds: 75 }));
    renderPage(<OverviewPage />);
    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent('No worker is running.');
    expect(notice).toHaveTextContent('last seen 1 min 15 s ago');
    expect(screen.getAllByText('No worker is running')).toHaveLength(1); // the Workers fact
  });

  it('shows the running worker processes and no warning', async () => {
    stub(overview({ alive: 2, last_seen_seconds: 1 }));
    renderPage(<OverviewPage />);
    expect(await screen.findByText('2 worker processes running')).toBeVisible();
    expect(screen.queryByText(/No worker is running/)).toBeNull();
  });
});
