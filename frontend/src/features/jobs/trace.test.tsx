import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { META, jsonResponse } from '../../test/fixtures';
import { renderPage } from '../../test/render';
import { TraceFact } from './JobDetailPage';
import { traceUrl } from './trace';

const TRACE = '4bf92f3577b34da6a3ce929d0e0e4736';

afterEach(() => vi.unstubAllGlobals());

describe('traceUrl', () => {
  it('fills the template', () => {
    expect(traceUrl('http://127.0.0.1:16686/trace/{trace_id}', TRACE)).toBe(`http://127.0.0.1:16686/trace/${TRACE}`);
  });

  it.each([
    [null, TRACE],
    ['http://viewer/{trace_id}', 'not-a-trace-id'],
    ['javascript:alert(1)//{trace_id}', TRACE],
    ['not a url {trace_id}', TRACE],
  ])('returns null for template %s and id %s', (template, id) => {
    expect(traceUrl(template, id)).toBeNull();
  });
});

function stubMeta(traceViewerUrl: string | null) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => jsonResponse({ ...META, trace_viewer_url: traceViewerUrl })),
  );
}

describe('TraceFact', () => {
  it('shows the trace id with a copy button and an Open trace link when a viewer is configured', async () => {
    stubMeta('http://127.0.0.1:16686/trace/{trace_id}');
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    renderPage(<TraceFact traceId={TRACE} />);

    const link = await screen.findByRole('link', { name: 'Open trace' });
    expect(link).toHaveAttribute('href', `http://127.0.0.1:16686/trace/${TRACE}`);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText(TRACE)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Copy trace id' }));
    expect(writeText).toHaveBeenCalledWith(TRACE);
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeVisible();
  });

  it('shows only the copyable trace id without a viewer', async () => {
    stubMeta(null);
    renderPage(<TraceFact traceId={TRACE} />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: 'Copy trace id' })).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Open trace' })).toBeNull();
  });

  it('explains a missing trace id', () => {
    stubMeta(null);
    renderPage(<TraceFact traceId={null} />);
    expect(screen.getByText(/tracing was off/)).toBeVisible();
  });
});
