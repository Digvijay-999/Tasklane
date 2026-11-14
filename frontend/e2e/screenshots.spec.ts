import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, request as playwrightRequest, test, type APIRequestContext, type Page } from '@playwright/test';
// Plain `test` (not the helpers' console-error guard): these runs break the network on purpose.
import { createJob, jobState, sample, waitFor } from './helpers';

// Screenshots of every screen and state, light and dark, for the design critique and
// docs/screenshots/. Real stack for real data; route mocking only for states that need a broken
// or empty server (empty database, API unreachable, stream limit). Run: npm run screenshots
const OUT = resolve(import.meta.dirname, '..', '..', 'docs', 'screenshots');
const REPO = resolve(import.meta.dirname, '..', '..');
const API = process.env.TASKLANE_API_URL ?? 'http://127.0.0.1:8000';
const THEMES = ['light', 'dark'] as const;

test.describe.configure({ mode: 'serial' });

const ids: Record<string, string> = {};
let api: APIRequestContext;

test.beforeAll(async () => {
  test.setTimeout(400_000);
  mkdirSync(OUT, { recursive: true });
  api = await playwrightRequest.newContext();
  ids.succeeded = await createJob(api, sample('report.pdf'));
  await waitFor(api, ids.succeeded, (j) => j.status === 'succeeded');
  ids.retried = await createJob(api, sample('report.pdf'), { fault_injection: { mode: 'transient', failures: 1 } });
  ids.scanned = await createJob(api, sample('scanned.pdf'));
  ids.repo = await createJob(api, sample('repo.zip'));
  for (const key of ['retried', 'scanned', 'repo']) {
    await waitFor(api, ids[key]!, (j) => ['succeeded', 'failed'].includes(j.status));
  }
  // Cancelled at a stage boundary.
  ids.cancelled = await createJob(api, sample('report.pdf'));
  await waitFor(api, ids.cancelled, (j) => j.current_stage === 'summarize');
  await api.post(`${API}/v1/jobs/${ids.cancelled}/cancel`);
  await waitFor(api, ids.cancelled, (j) => j.status === 'cancelled');
  // Worker killed mid-job: the lease expires and recovery runs it again.
  ids.recovered = await createJob(api, sample('report.pdf'));
  await waitFor(api, ids.recovered, (j) => j.current_stage === 'summarize');
  execFileSync('docker', ['compose', 'kill', 'worker'], { cwd: REPO, stdio: 'ignore' });
  execFileSync('docker', ['compose', 'start', 'worker'], { cwd: REPO, stdio: 'ignore' });
  await waitFor(api, ids.recovered, (j) => j.status === 'succeeded', 180_000);
  // A long job that stays running while the live screenshots are taken.
  ids.running = await createJob(api, sample('large.pdf'));
  await waitFor(api, ids.running, (j) => j.current_stage === 'summarize', 120_000);
});

test.afterAll(async () => {
  if (ids.running) await api.post(`${API}/v1/jobs/${ids.running}/cancel`);
  await api.dispose();
});

async function shoot(page: Page, name: string, open: () => Promise<void>, { fullPage = true } = {}) {
  for (const theme of THEMES) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await open();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: resolve(OUT, `${name}-${theme}.png`), fullPage });
  }
}

const finished = (page: Page) => expect(page.locator('.live')).toContainText('Job finished');
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('overview', async ({ page }) => {
  await shoot(page, 'overview', async () => {
    await page.goto('/');
    await expect(page.locator('.count-value').first()).not.toBeEmpty();
    await expect(page.locator('.jobs-table tbody tr').first()).toBeVisible();
  });
});

test('overview: empty, loading and API unreachable', async ({ page }) => {
  const empty = {
    counts: { queued: 0, running: 0, retry_wait: 0, cancel_requested: 0, succeeded: 0, failed: 0, cancelled: 0 },
    total: 0,
    oldest_queued: null,
    queue: { broker: 'ok', pending_messages: 0, scheduled_messages: 0, failing_messages: 0, max_dispatch_attempts: 0, last_dispatch_error: null, oldest_pending_seconds: null },
    database_time: new Date().toISOString(),
  };
  await page.route('**/v1/overview', (route) => route.fulfill(json(empty)));
  await page.route('**/v1/jobs?*', (route) => route.fulfill(json({ items: [], next_cursor: null })));
  await shoot(page, 'overview-empty', async () => {
    await page.goto('/');
    await expect(page.getByText('No jobs yet.')).toBeVisible();
  });
  await page.unrouteAll();

  await page.route('**/v1/overview', () => new Promise(() => {})); // never answers
  await page.route('**/v1/jobs?*', () => new Promise(() => {}));
  await shoot(page, 'overview-loading', async () => {
    await page.goto('/');
    await expect(page.locator('.skeleton').first()).toBeVisible();
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });

  await shoot(page, 'overview-api-unreachable', async () => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await page.goto('/');
    await expect(page.locator('.count-value').first()).not.toBeEmpty();
    await page.route(/\/(v1|health)\//, (route) => route.abort('connectionrefused'));
    await expect(page.getByRole('alert').filter({ hasText: "Can't reach the Tasklane API" })).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('.stale-note').first()).toBeVisible({ timeout: 20_000 });
  });
});

test('jobs list: all, filtered, no match', async ({ page }) => {
  await shoot(page, 'jobs', async () => {
    await page.goto('/jobs');
    await expect(page.locator('.jobs-table tbody tr').first()).toBeVisible();
  });
  await shoot(page, 'jobs-filtered', async () => {
    await page.goto('/jobs?workflow=pdf_ingest&status=failed&status=cancelled');
    await expect(page.locator('.jobs-table tbody tr').first()).toBeVisible();
  });
  await page.route('**/v1/jobs?*', (route) => route.fulfill(json({ items: [], next_cursor: null })));
  await shoot(page, 'jobs-no-match', async () => {
    await page.goto('/jobs?status=cancel_requested');
    await expect(page.getByText('No jobs match these filters.')).toBeVisible();
  });
});

test('new job: form, validation, upload progress, idempotent replay', async ({ page }) => {
  await shoot(page, 'new-job', async () => {
    await page.goto('/jobs/new');
    await page.getByRole('radio', { name: 'Fail the first attempts (transient)' }).check();
  });
  await shoot(page, 'new-job-validation', async () => {
    await page.goto('/jobs/new');
    await page.getByLabel('File', { exact: true }).setInputFiles(sample('repo.zip'));
    await expect(page.getByRole('alert')).toContainText('needs a PDF file');
  });
  await page.route('**/v1/uploads?*', () => new Promise(() => {})); // upload never completes
  await shoot(page, 'new-job-uploading', async () => {
    await page.goto('/jobs/new');
    await page.getByLabel('File', { exact: true }).setInputFiles(sample('report.pdf'));
    await page.getByRole('button', { name: 'Upload and start job' }).click();
    await expect(page.getByRole('progressbar', { name: 'Upload progress' })).toBeVisible();
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  await page.route('**/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const job = await (await api.get(`${API}/v1/jobs/${ids.succeeded}`)).json();
    await route.fulfill({ ...json(job), headers: { 'Idempotent-Replayed': 'true' } });
  });
  await shoot(page, 'new-job-replayed', async () => {
    await page.goto('/jobs/new');
    await page.getByLabel('File', { exact: true }).setInputFiles(sample('report.pdf'));
    await page.getByRole('button', { name: 'Upload and start job' }).click();
    await expect(page.getByText(/already had this submission/)).toBeVisible();
  });
});

test('job detail: finished jobs', async ({ page }) => {
  await shoot(page, 'job-succeeded-pdf', async () => {
    await page.goto(`/jobs/${ids.succeeded}`);
    await finished(page);
    await expect(page.getByLabel('Summary preview')).not.toBeEmpty();
  });
  await shoot(page, 'job-retried-fault-injection', async () => {
    await page.goto(`/jobs/${ids.retried}`);
    await finished(page);
  });
  await shoot(page, 'job-failed-scanned-pdf', async () => {
    await page.goto(`/jobs/${ids.scanned}`);
    await finished(page);
  });
  await shoot(page, 'job-cancelled', async () => {
    await page.goto(`/jobs/${ids.cancelled}`);
    await finished(page);
  });
  await shoot(page, 'job-recovered-after-worker-kill', async () => {
    await page.goto(`/jobs/${ids.recovered}`);
    await finished(page);
    await expect(page.locator('.tl-gap[data-kind="lease_expired"]')).toBeVisible();
  });
  await shoot(page, 'job-repo-manifest', async () => {
    await page.goto(`/jobs/${ids.repo}`);
    await finished(page);
    await expect(page.getByRole('heading', { name: 'Skip reasons' })).toBeVisible();
  });
  await shoot(page, 'job-not-found', async () => {
    await page.goto('/jobs/00000000-0000-4000-8000-000000000000');
    await expect(page.getByRole('heading', { name: 'Job not found' })).toBeVisible();
  });
});

test('job detail: live states of a running job', async ({ page }) => {
  expect((await jobState(api, ids.running!)).status).toBe('running');
  await shoot(page, 'job-running-live', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await expect(page.locator('.live')).toContainText('Live');
    await expect(page.locator('.tl-cell[data-state="running"]')).toBeVisible();
  });
  await shoot(page, 'job-cancel-dialog', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await page.getByRole('button', { name: 'Cancel job' }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
  }, { fullPage: false });

  // The connection drops (the browser keeps retrying): "paused, reconnecting".
  await page.route(/\/events\?after=/, (route) => route.abort('connectionreset'));
  await shoot(page, 'job-stream-reconnecting', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await expect(page.locator('.live')).toContainText('reconnecting', { timeout: 20_000 });
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });

  // The server's stream limit is reached (503): the page polls the history route instead.
  await page.route(/\/events\?after=/, (route) =>
    route.fulfill(json({ error: { code: 'stream_limit_reached', message: 'Too many open event streams.', details: { limit: 50 }, request_id: null } }, 503)),
  );
  await shoot(page, 'job-stream-limit-polling', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await expect(page.locator('.live')).toContainText('Checking for new events every 2 s');
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('narrow window', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  await shoot(page, 'narrow-job-retried', async () => {
    await page.goto(`/jobs/${ids.retried}`);
    await finished(page);
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await shoot(page, 'phone-job-retried', async () => {
    await page.goto(`/jobs/${ids.retried}`);
    await finished(page);
  });
});
