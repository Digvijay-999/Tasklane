import AxeBuilder from '@axe-core/playwright';
import { expect, request as playwrightRequest, test, type APIRequestContext, type Page } from '@playwright/test';
import { createJob, jobState, sample, waitFor } from './helpers';

// Automated accessibility audit (axe-core) of every dashboard screen and its main states, in the
// light and the dark theme. Part of `npm run e2e`, so a new violation fails the suite. Rules:
// WCAG 2.0/2.1/2.2 levels A and AA, plus axe's best practices. Real stack for real data; route
// mocking only for states that need an empty or broken server (as in screenshots.spec.ts).
const API = process.env.TASKLANE_API_URL ?? 'http://127.0.0.1:8000';
const THEMES = ['light', 'dark'] as const;
const RULES = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22a', 'wcag22aa', 'best-practice'];

test.describe.configure({ mode: 'serial' });

const ids: Record<string, string> = {};
let api: APIRequestContext;

test.beforeAll(async () => {
  test.setTimeout(300_000);
  api = await playwrightRequest.newContext();
  ids.succeeded = await createJob(api, sample('report.pdf'));
  ids.retried = await createJob(api, sample('report.pdf'), { fault_injection: { mode: 'transient', failures: 1 } });
  ids.scanned = await createJob(api, sample('scanned.pdf'));
  ids.repo = await createJob(api, sample('repo.zip'));
  for (const key of ['succeeded', 'retried', 'scanned', 'repo']) {
    await waitFor(api, ids[key]!, (j) => ['succeeded', 'failed'].includes(j.status));
  }
  ids.cancelled = await createJob(api, sample('report.pdf'));
  await waitFor(api, ids.cancelled, (j) => j.current_stage === 'summarize');
  await api.post(`${API}/v1/jobs/${ids.cancelled}/cancel`);
  await waitFor(api, ids.cancelled, (j) => j.status === 'cancelled');
  ids.running = await createJob(api, sample('medium.pdf')); // ~40 s in summarize
  await waitFor(api, ids.running, (j) => j.current_stage === 'summarize', 120_000);
});

test.afterAll(async () => {
  if (ids.running) await api.post(`${API}/v1/jobs/${ids.running}/cancel`);
  await api.dispose();
});

/** Open the state in each theme and fail with a readable list of every violation. */
async function audit(page: Page, name: string, open: () => Promise<void>) {
  const problems: string[] = [];
  for (const theme of THEMES) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await open();
    const results = await new AxeBuilder({ page }).withTags(RULES).analyze();
    for (const violation of results.violations) {
      const where = violation.nodes.map((node) => node.target.join(' ')).join(' | ');
      problems.push(`${name} [${theme}] ${violation.id} (${violation.impact}): ${violation.help} -> ${where}`);
    }
  }
  // Soft: one run reports every state's violations, not only the first one's.
  expect.soft(problems, problems.join('\n')).toEqual([]);
}

const finished = (page: Page) => expect(page.locator('.live')).toContainText('Job finished');
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });

// First, while the medium-sized job is still in its ~40 s summarize stage.
test('job detail: a running job, its cancel dialog and stream states', async ({ page }) => {
  expect((await jobState(api, ids.running!)).status).toBe('running');
  await audit(page, 'job-running-live', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await expect(page.locator('.live')).toContainText('Live');
    await expect(page.locator('.tl-cell[data-state="running"]')).toBeVisible();
  });
  await audit(page, 'job-cancel-dialog', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await page.getByRole('button', { name: 'Cancel job' }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
  });

  await page.route(/\/events\?after=/, (route) => route.abort('connectionreset'));
  await audit(page, 'job-stream-reconnecting', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await expect(page.locator('.live')).toContainText('reconnecting', { timeout: 20_000 });
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });

  await page.route(/\/events\?after=/, (route) =>
    route.fulfill(json({ error: { code: 'stream_limit_reached', message: 'Too many open event streams.', details: { limit: 50 }, request_id: null } }, 503)),
  );
  await audit(page, 'job-stream-limit-polling', async () => {
    await page.goto(`/jobs/${ids.running}`);
    await expect(page.locator('.live')).toContainText('Checking for new events every 2 s');
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('overview: data, empty, loading, API unreachable', async ({ page }) => {
  await audit(page, 'overview', async () => {
    await page.goto('/');
    await expect(page.locator('.count-value').first()).not.toBeEmpty();
    await expect(page.locator('.jobs-table tbody tr').first()).toBeVisible();
  });

  const empty = {
    counts: { queued: 0, running: 0, retry_wait: 0, cancel_requested: 0, succeeded: 0, failed: 0, cancelled: 0 },
    total: 0,
    oldest_queued: null,
    queue: { broker: 'ok', pending_messages: 0, scheduled_messages: 0, failing_messages: 0, max_dispatch_attempts: 0, last_dispatch_error: null, oldest_pending_seconds: null },
    workers: { alive: 0, last_seen_seconds: null },
    database_time: new Date().toISOString(),
  };
  await page.route('**/v1/overview', (route) => route.fulfill(json(empty)));
  await page.route('**/v1/jobs?*', (route) => route.fulfill(json({ items: [], next_cursor: null })));
  await audit(page, 'overview-empty-no-worker', async () => {
    await page.goto('/');
    await expect(page.getByText('No jobs yet.')).toBeVisible();
  });
  await page.unrouteAll();

  await page.route('**/v1/overview', () => new Promise(() => {}));
  await page.route('**/v1/jobs?*', () => new Promise(() => {}));
  await audit(page, 'overview-loading', async () => {
    await page.goto('/');
    await expect(page.locator('.skeleton').first()).toBeVisible();
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });

  await audit(page, 'overview-api-unreachable', async () => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await page.goto('/');
    await expect(page.locator('.count-value').first()).not.toBeEmpty();
    await page.route(/\/(v1|health)\//, (route) => route.abort('connectionrefused'));
    await expect(page.getByRole('alert').filter({ hasText: "Can't reach the Tasklane API" })).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('.stale-note').first()).toBeVisible({ timeout: 20_000 });
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('jobs list: all, filtered, no match', async ({ page }) => {
  await audit(page, 'jobs', async () => {
    await page.goto('/jobs');
    await expect(page.locator('.jobs-table tbody tr').first()).toBeVisible();
  });
  await audit(page, 'jobs-filtered', async () => {
    await page.goto('/jobs?workflow=pdf_ingest&status=failed&status=cancelled');
    await expect(page.locator('.jobs-table tbody tr').first()).toBeVisible();
  });
  await audit(page, 'jobs-no-match', async () => {
    await page.goto('/jobs?status=cancel_requested&workflow=repo_index');
    await expect(page.getByText('No jobs match these filters.')).toBeVisible();
  });
});

test('new job: form, fault options, validation error, uploading, idempotent replay', async ({ page }) => {
  await audit(page, 'new-job', async () => {
    await page.goto('/jobs/new');
    await page.getByRole('radio', { name: 'Fail the first attempts (transient)' }).check();
  });
  await audit(page, 'new-job-repo-validation', async () => {
    await page.goto('/jobs/new');
    await page.getByLabel('File', { exact: true }).setInputFiles(sample('repo.zip'));
    await expect(page.getByRole('alert')).toContainText('needs a PDF file');
  });
  await page.route('**/v1/uploads?*', () => new Promise(() => {}));
  await audit(page, 'new-job-uploading', async () => {
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
  await audit(page, 'new-job-replayed', async () => {
    await page.goto('/jobs/new');
    await page.getByLabel('File', { exact: true }).setInputFiles(sample('report.pdf'));
    await page.getByRole('button', { name: 'Upload and start job' }).click();
    await expect(page.getByText(/already had this submission/)).toBeVisible();
  });
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('job detail: finished jobs', async ({ page }) => {
  for (const [name, key] of [
    ['job-succeeded-pdf', 'succeeded'],
    ['job-retried-fault-injection', 'retried'],
    ['job-failed-scanned-pdf', 'scanned'],
    ['job-cancelled', 'cancelled'],
    ['job-repo-manifest', 'repo'],
  ] as const) {
    await audit(page, name, async () => {
      await page.goto(`/jobs/${ids[key]}`);
      await finished(page);
    });
  }
  await audit(page, 'job-not-found', async () => {
    await page.goto('/jobs/00000000-0000-4000-8000-000000000000');
    await expect(page.getByRole('heading', { name: 'Job not found' })).toBeVisible();
  });
  await audit(page, 'page-not-found', async () => {
    await page.goto('/no-such-page');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  });
});

test('narrow and phone widths', async ({ page }) => {
  for (const [name, width, height] of [
    ['narrow-job', 1024, 800],
    ['phone-job', 390, 844],
    ['phone-overview', 390, 844],
  ] as const) {
    await page.setViewportSize({ width, height });
    await audit(page, name, async () => {
      if (name === 'phone-overview') {
        await page.goto('/');
        await expect(page.locator('.count-value').first()).not.toBeEmpty();
      } else {
        await page.goto(`/jobs/${ids.retried}`);
        await finished(page);
      }
    });
  }
});
