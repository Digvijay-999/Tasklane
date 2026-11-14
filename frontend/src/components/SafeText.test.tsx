import { render, screen } from '@testing-library/react';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';
import { JobsTable } from '../features/jobs/JobsTable';
import { parseRepoManifest } from '../features/jobs/Manifest';
import type { JobListItem } from '../lib/api/types';
import { jobDetail } from '../test/fixtures';
import { SafeText } from './SafeText';

const HOSTILE = [
  'invoice‮gpj.pdf', // right-to-left override: would display as "invoicefdp.jpg"
  'javascript:alert(1).pdf',
  '<img src=x onerror=alert(1)>.pdf',
  'https://evil.example/x.pdf',
  'zero​width⁦isolate.pdf',
  'tab\there\u0007bell.pdf',
];

describe('SafeText', () => {
  it('renders hostile file names as inert text with visible escapes', () => {
    const { container } = render(
      <ul>
        {HOSTILE.map((name) => (
          <li key={name}>
            <SafeText text={name} />
          </li>
        ))}
      </ul>,
    );
    // Nothing became markup or a link.
    expect(container.querySelector('img, a, script')).toBeNull();
    const items = [...container.querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual([
      'invoice\\u202Egpj.pdf',
      'javascript:alert(1).pdf',
      '<img src=x onerror=alert(1)>.pdf',
      'https://evil.example/x.pdf',
      'zero\\u200Bwidth\\u2066isolate.pdf',
      'tab\\u0009here\\u0007bell.pdf',
    ]);
    // Each name is isolated so right-to-left text can't reorder its neighbours.
    expect(container.querySelectorAll('bdi')).toHaveLength(HOSTILE.length);
    // No raw invisible character survives.
    expect(container.textContent).not.toMatch(/[‮​⁦\u0007\t]/);
  });

  it('keeps line breaks in multiline mode (summary preview) but still escapes the rest', () => {
    const { container } = render(<SafeText text={'line one\nline‮two'} multiline />);
    expect(container.textContent).toBe('line one\nline\\u202Etwo');
  });

  it('keeps hostile names inert inside the jobs table', () => {
    const jobs: JobListItem[] = HOSTILE.map((name, index) => ({
      ...jobDetail({ id: `00000000-0000-4000-8000-00000000000${index}` }),
      upload_filename: name,
    }));
    const router = createMemoryRouter([{ path: '/', element: <JobsTable jobs={jobs} loading={false} caption="jobs" /> }]);
    const { container } = render(<RouterProvider router={router} />);
    // The only links are the row links to /jobs/<id>; no file name became an href.
    const hrefs = screen.getAllByRole('link').map((a) => a.getAttribute('href'));
    expect(hrefs.every((href) => href?.startsWith('/jobs/'))).toBe(true);
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('invoice\\u202Egpj.pdf');
  });

  it('reads a manifest defensively: wrong types become empty values, paths stay strings', () => {
    const manifest = parseRepoManifest(
      JSON.stringify({
        totals: { files: '3', included: 1 },
        languages: { Python: { files: 1, lines: 'x' } },
        included: [{ path: '<b>a</b>.py', language: 'Python', bytes: 1, lines: 1 }, 'junk'],
        skipped: [{ path: 'javascript:x', reason: 'hidden' }],
      }),
    );
    expect(manifest.totals).toEqual({ files: 0, included: 1, skipped: 0, included_bytes: 0, included_lines: 0 });
    expect(manifest.languages).toEqual([['Python', { files: 1, lines: 0, bytes: 0 }]]);
    expect(manifest.included).toEqual([
      { path: '<b>a</b>.py', language: 'Python', bytes: 1, lines: 1 },
      { path: '', language: '', bytes: 0, lines: 0 },
    ]);
    expect(manifest.skipped).toEqual([{ path: 'javascript:x', reason: 'hidden' }]);
  });
});

describe('source rules', () => {
  const files = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? files(path) : [path];
    });
  const source = files(join(__dirname, '..')).filter((f) => /\.(tsx?|css)$/.test(f) && !f.endsWith('.test.tsx'));

  it('never uses dangerouslySetInnerHTML', () => {
    const prop = new RegExp(String.raw`dangerouslySetInner` + String.raw`HTML\s*[=:]`);
    const offenders = source.filter((f) => prop.test(readFileSync(f, 'utf8')));
    expect(offenders).toEqual([]);
  });

  it('contains no emoji', () => {
    const offenders = source.filter((f) => /\p{Extended_Pictographic}/u.test(readFileSync(f, 'utf8')));
    expect(offenders).toEqual([]);
  });
});
