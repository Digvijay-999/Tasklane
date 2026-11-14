import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { uploadFile } from '../../lib/api/upload';
import { META, jobDetail, jsonResponse } from '../../test/fixtures';
import { renderPage } from '../../test/render';
import { NewJobPage } from './NewJobPage';

vi.mock('../../lib/api/upload', () => ({ uploadFile: vi.fn() }));

const pdfFile = () => new File(['%PDF-1.7 hello'], 'report.pdf', { type: 'application/pdf' });

/** Route fetch calls by URL; `createJob` answers POST /v1/jobs. */
function stubApi(createJob: (init: RequestInit) => Promise<Response>) {
  const calls: RequestInit[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === '/v1/meta') return jsonResponse(META);
      if (url === '/v1/jobs' && init.method === 'POST') {
        calls.push(init);
        return createJob(init);
      }
      throw new Error(`unexpected request ${url}`);
    }),
  );
  return calls;
}

const header = (init: RequestInit, name: string) => (init.headers as Record<string, string>)[name];

async function chooseAndSubmit() {
  await userEvent.upload(await screen.findByLabelText('File'), pdfFile());
  await userEvent.click(screen.getByRole('button', { name: 'Upload and start job' }));
}

beforeEach(() => {
  vi.mocked(uploadFile).mockReturnValue({
    promise: Promise.resolve({ id: 'upload-1' } as never),
    abort: vi.fn(),
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(uploadFile).mockReset();
});

describe('NewJobPage', () => {
  it('validates against the limits from the API before uploading anything', async () => {
    stubApi(async () => jsonResponse({}));
    renderPage(<NewJobPage />, '/jobs/new');
    const input = await screen.findByLabelText('File');
    await userEvent.upload(input, new File(['x'.repeat(1001)], 'big.pdf'));
    expect(screen.getByRole('alert')).toHaveTextContent('PDF uploads are limited to 1000 B');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Upload and start job' })).toBeDisabled();
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it('says clearly when the server returned an existing job (Idempotent-Replayed)', async () => {
    stubApi(async () => jsonResponse(jobDetail({ id: 'existing-job' }), 200, { 'Idempotent-Replayed': 'true' }));
    renderPage(<NewJobPage />, '/jobs/new');
    await chooseAndSubmit();
    expect(await screen.findByText(/already had this submission, so it returned the existing job/)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Open the existing job' })).toHaveAttribute('href', '/jobs/existing-job');
  });

  it('reuses the same Idempotency-Key and upload when retrying after a network error', async () => {
    let attempt = 0;
    const calls = stubApi(async () => {
      attempt++;
      if (attempt === 1) throw new TypeError('Failed to fetch'); // the response never arrived
      return jsonResponse(jobDetail({ id: 'new-job' }), 201, { 'Idempotent-Replayed': 'false' });
    });
    renderPage(<NewJobPage />, '/jobs/new');
    await chooseAndSubmit();
    expect(await screen.findByText(/cannot create a second job/)).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('job page')).toBeVisible(); // navigated to /jobs/new-job
    expect(calls).toHaveLength(2);
    expect(header(calls[0]!, 'Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
    expect(header(calls[1]!, 'Idempotency-Key')).toBe(header(calls[0]!, 'Idempotency-Key'));
    expect(calls[1]!.body).toBe(calls[0]!.body);
    expect(uploadFile).toHaveBeenCalledTimes(1); // the file was not uploaded twice
  });

  it('uses a new key for a different submission', async () => {
    const calls = stubApi(async () => {
      throw new TypeError('Failed to fetch');
    });
    renderPage(<NewJobPage />, '/jobs/new');
    await chooseAndSubmit();
    await screen.findByText(/cannot create a second job/);
    await userEvent.upload(screen.getByLabelText('File'), new File(['%PDF-other'], 'other.pdf'));
    await userEvent.click(screen.getByRole('button', { name: 'Upload and start job' }));
    await screen.findByText(/cannot create a second job/);
    expect(header(calls[1]!, 'Idempotency-Key')).not.toBe(header(calls[0]!, 'Idempotency-Key'));
  });

  it('shows the server message and code when the upload is refused', async () => {
    const { ApiError } = await import('../../lib/api/client');
    vi.mocked(uploadFile).mockImplementation(() => ({
      promise: Promise.reject(
        new ApiError(415, { code: 'file_signature_mismatch', message: "The file doesn't start like a PDF.", details: {}, request_id: null }),
      ),
      abort: vi.fn(),
    }));
    stubApi(async () => jsonResponse({}));
    renderPage(<NewJobPage />, '/jobs/new');
    await chooseAndSubmit();
    expect(await screen.findByRole('alert')).toHaveTextContent("The file doesn't start like a PDF. file_signature_mismatch");
  });
});
