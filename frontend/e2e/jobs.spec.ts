import { readFileSync } from 'node:fs';
import { expect, finalRow, jobState, sample, submitJob, test, waitFor } from './helpers';

// Real stack, real worker. Needs the demo settings (STUB_DELAY_SECONDS=1 keeps the summarize
// stage running for a few seconds, long enough to cancel or reload in the middle).

test('a PDF job runs live to succeeded and its summary downloads', async ({ page }) => {
  await submitJob(page, sample('report.pdf'));
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('report.pdf');
  // Live: the timeline shows a running stage before the end.
  await expect(page.locator('.tl-cell[data-state="running"]')).toHaveCount(1, { timeout: 30_000 });
  await expect(page.locator('.live')).toContainText('Live');
  await expect(finalRow(page)).toContainText('Succeeded in', { timeout: 90_000 });
  await expect(page.locator('.live')).toContainText('Job finished. Live updates stopped.');
  await expect(page.locator('.tl-lane')).toHaveCount(1);
  await expect(page.locator('.tl-lane .tl-cell[data-state="done"]')).toHaveCount(5);

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Download summary' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('summary.md');
  const text = readFileSync((await download.path())!, 'utf8');
  expect(text).toMatch(/^# /);
  await expect(page.getByLabel('Summary preview')).toContainText(text.split('\n')[0]!);
});

test('a scanned PDF fails with the right message and Retry disabled with its reason', async ({ page }) => {
  await submitJob(page, sample('scanned.pdf'));
  await expect(finalRow(page)).toContainText('Failed', { timeout: 60_000 });
  const error = page.getByRole('region', { name: 'Why it failed' });
  await expect(error).toContainText('pdf_no_text');
  await expect(error).toContainText('little or no extractable text');
  await expect(page.locator('.tl-cell[data-state="failed"]')).toContainText('permanent');
  const retry = page.getByRole('button', { name: 'Retry job' });
  await expect(retry).toBeDisabled();
  await expect(retry).toHaveAccessibleDescription(/failed because of its input file/);
  await expect(page.locator('.tl-lane')).toHaveCount(1); // permanent: never retried
});

test('cancelling a running job stops it at a stage boundary', async ({ page, request }) => {
  const id = await submitJob(page, sample('report.pdf'));
  await waitFor(request, id, (job) => job.current_stage === 'summarize');
  await page.getByRole('button', { name: 'Cancel job' }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toContainText('The worker finishes the stage it is in');
  await dialog.getByRole('button', { name: 'Cancel job' }).click();
  await expect(finalRow(page)).toContainText('Cancelled before Write artifacts', { timeout: 60_000 });
  await expect(page.locator('.tl-cell[data-state="not_run"]')).toHaveCount(1);
  await expect(page.locator('.job-facts')).toContainText('Cancelled');
  await expect(page.getByRole('button', { name: 'Cancel job' })).toBeDisabled();
  await expect(page.getByText('The job has finished, so there is nothing to cancel.')).toBeVisible();
});

test('reloading mid-job resumes the timeline without duplicates or gaps', async ({ page, request }) => {
  const id = await submitJob(page, sample('report.pdf'));
  await waitFor(request, id, (job) => job.current_stage === 'summarize');
  await page.reload();
  await expect(page.locator('.live')).toContainText(/Live|Connecting/);
  await expect(finalRow(page)).toContainText('Succeeded in', { timeout: 90_000 });

  await page.getByLabel('Show progress events').check();
  const seqs = (await page.locator('.events-table tbody tr td:first-child').allTextContents()).map(Number);
  const { last_event_seq } = await jobState(request, id);
  expect(seqs).toEqual(Array.from({ length: last_event_seq }, (_, i) => i + 1));
  await expect(page.locator('.tl-lane .tl-cell[data-state="done"]')).toHaveCount(5);
});

test('a fault-injected job shows its retry live', async ({ page }) => {
  await submitJob(page, sample('report.pdf'), { failures: 1 });
  await expect(page.locator('.tl-cell[data-state="failed"]')).toContainText('injected_transient', { timeout: 60_000 });
  await expect(page.locator('.tl-gap[data-kind="retry_wait"]')).toContainText('transient error');
  await expect(page.locator('.tl-lane')).toHaveCount(2, { timeout: 60_000 });
  await expect(finalRow(page)).toContainText('Succeeded in', { timeout: 90_000 });
  const second = page.locator('.tl-lane').nth(1);
  await expect(second).toContainText('Automatic retry');
  await expect(second.locator('.tl-cell[data-state="reused"]')).toHaveCount(3); // checkpoints
  await expect(page.locator('.tl-gap[data-kind="retry_wait"]')).toContainText(/Waited .* then retried automatically/);
  await expect(page.getByRole('table', { name: 'Attempts', exact: true })).toContainText('Failed, retryable');
});

test('a repository ZIP shows the manifest viewer with skip reasons', async ({ page }) => {
  await submitJob(page, sample('repo.zip'), { workflow: 'Repository ZIP indexing' });
  await expect(finalRow(page)).toContainText('Succeeded in', { timeout: 90_000 });
  const manifest = page.getByRole('region', { name: 'Manifest' });
  await expect(manifest.getByRole('heading', { name: 'Skip reasons' })).toBeVisible();
  for (const reason of ['Excluded folder', 'Symbolic link', 'Nested archive', 'Hidden file', 'Binary']) {
    await expect(manifest).toContainText(reason);
  }
  await expect(manifest.getByRole('heading', { name: /Indexed files/ })).toBeVisible();
  await expect(manifest).toContainText('src/shop/orders.py');
  // Filter the skipped list by path.
  await manifest.getByLabel('Filter skipped files by path').fill('node_modules');
  await expect(manifest.getByRole('table').last().locator('tbody tr')).toHaveCount(1);
});
