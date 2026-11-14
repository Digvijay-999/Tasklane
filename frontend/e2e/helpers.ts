import { expect, test as base, type APIRequestContext, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { SAMPLES } from './global-setup';

export const sample = (name: string) => resolve(SAMPLES, name);
const API = process.env.TASKLANE_API_URL ?? 'http://127.0.0.1:8000';

/** Every test also fails on a console error (a CSP violation shows up there). */
export const test = base.extend<{ consoleErrors: string[] }>({
  consoleErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(message.text());
      });
      page.on('pageerror', (error) => errors.push(error.message));
      await use(errors);
      expect(errors.filter((e) => !e.includes('net::ERR_ABORTED') && !e.includes('status of 503') && !e.includes('status of 404'))).toEqual([]);
    },
    { auto: true },
  ],
});
export { expect };

/** Submit a job through the New job page; returns the job id from the URL it lands on. */
export async function submitJob(
  page: Page,
  file: string,
  { workflow = 'PDF ingestion', failures }: { workflow?: string; failures?: number } = {},
): Promise<string> {
  await page.goto('/jobs/new');
  await page.getByRole('radio', { name: new RegExp(workflow) }).check();
  await page.getByLabel('File', { exact: true }).setInputFiles(file);
  if (failures) {
    await page.getByRole('radio', { name: 'Fail the first attempts (transient)' }).check();
    await page.getByLabel('Failing attempts').fill(String(failures));
  }
  await page.getByRole('button', { name: 'Upload and start job' }).click();
  await page.waitForURL(/\/jobs\/[0-9a-f-]{36}$/);
  return page.url().split('/').pop()!;
}

/** Create a job straight through the API (for screenshots that only need the result). */
export async function createJob(
  request: APIRequestContext,
  file: string,
  options?: Record<string, unknown>,
): Promise<string> {
  const pdf = file.endsWith('.pdf');
  const name = basename(file);
  const upload = await request.post(`${API}/v1/uploads?filename=${encodeURIComponent(name)}`, {
    headers: { 'Content-Type': pdf ? 'application/pdf' : 'application/zip' },
    data: readFileSync(file),
  });
  expect(upload.status()).toBe(201);
  const job = await request.post(`${API}/v1/jobs`, {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: { workflow_type: pdf ? 'pdf_ingest' : 'repo_index', upload_id: (await upload.json()).id, ...(options ? { options } : {}) },
  });
  expect(job.status()).toBe(201);
  return (await job.json()).id;
}

export async function jobState(request: APIRequestContext, id: string) {
  return (await (await request.get(`${API}/v1/jobs/${id}`)).json()) as {
    status: string;
    last_event_seq: number;
    current_stage: string | null;
    attempts: { attempt_no: number; outcome: string | null }[];
  };
}

export async function waitFor(
  request: APIRequestContext,
  id: string,
  done: (job: Awaited<ReturnType<typeof jobState>>) => boolean,
  timeoutMs = 120_000,
) {
  await expect
    .poll(async () => done(await jobState(request, id)), { timeout: timeoutMs, intervals: [250] })
    .toBe(true);
}

export const finalRow = (page: Page) => page.locator('.tl-final');
