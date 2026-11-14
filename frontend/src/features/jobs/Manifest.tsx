import { useQuery } from '@tanstack/react-query';
import { useId, useMemo, useState } from 'react';
import { SafeText } from '../../components/SafeText';
import { ErrorNotice, Skeleton } from '../../components/ui';
import { api } from '../../lib/api/client';
import type { Artifact } from '../../lib/api/types';
import { formatBytes } from '../../lib/format';
import { SKIP_REASON_LABEL } from '../../lib/labels';

// The manifest is untrusted data: paths come from inside the uploaded ZIP. Everything below is
// read defensively (wrong types become empty) and every path is rendered with <SafeText>.

interface IncludedFile {
  path: string;
  language: string;
  bytes: number;
  lines: number;
}
interface SkippedFile {
  path: string;
  reason: string;
}
interface RepoManifest {
  totals: { files: number; included: number; skipped: number; included_bytes: number; included_lines: number };
  languages: [string, { files: number; lines: number; bytes: number }][];
  skipReasons: [string, number][];
  included: IncludedFile[];
  skipped: SkippedFile[];
}

const MAX_MANIFEST_BYTES = 16 * 1024 * 1024;
const PAGE = 100;

const n = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
const s = (value: unknown) => (typeof value === 'string' ? value : '');
const obj = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
const arr = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

export function parseRepoManifest(text: string): RepoManifest {
  const raw = obj(JSON.parse(text));
  const totals = obj(raw.totals);
  return {
    totals: {
      files: n(totals.files),
      included: n(totals.included),
      skipped: n(totals.skipped),
      included_bytes: n(totals.included_bytes),
      included_lines: n(totals.included_lines),
    },
    languages: Object.entries(obj(raw.languages))
      .map(([name, stats]): [string, { files: number; lines: number; bytes: number }] => {
        const v = obj(stats);
        return [name, { files: n(v.files), lines: n(v.lines), bytes: n(v.bytes) }];
      })
      .sort((a, b) => b[1].files - a[1].files || a[0].localeCompare(b[0])),
    skipReasons: Object.entries(obj(raw.skip_reasons))
      .map(([reason, count]): [string, number] => [reason, n(count)])
      .sort((a, b) => b[1] - a[1]),
    included: arr(raw.included).map((f) => {
      const v = obj(f);
      return { path: s(v.path), language: s(v.language), bytes: n(v.bytes), lines: n(v.lines) };
    }),
    skipped: arr(raw.skipped).map((f) => {
      const v = obj(f);
      return { path: s(v.path), reason: s(v.reason) };
    }),
  };
}

function FileList<T extends { path: string }>({
  title,
  files,
  columns,
  render,
}: {
  title: string;
  files: T[];
  columns: string[];
  render: (file: T, index: number) => React.ReactNode;
}) {
  const filterId = useId();
  const [filter, setFilter] = useState('');
  const [shown, setShown] = useState(PAGE);
  const matching = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? files.filter((file) => file.path.toLowerCase().includes(needle)) : files;
  }, [files, filter]);
  return (
    <div className="file-list">
      <div className="section-head">
        <h3>
          {title} <span className="muted num">{files.length}</span>
        </h3>
        <label htmlFor={filterId} className="visually-hidden">
          Filter {title.toLowerCase()} by path
        </label>
        <input
          id={filterId}
          type="search"
          className="input input-small"
          placeholder="Filter by path"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setShown(PAGE);
          }}
        />
      </div>
      {matching.length === 0 ? (
        <p className="muted">{files.length === 0 ? 'None.' : 'No paths match this filter.'}</p>
      ) : (
        <table className="table table-compact">
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th key={column} scope="col" className={index === 0 ? undefined : 'num'}>
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>{matching.slice(0, shown).map((file, index) => render(file, index))}</tbody>
        </table>
      )}
      {matching.length > shown && (
        <button type="button" className="button" data-variant="ghost" onClick={() => setShown(shown + PAGE)}>
          Show {Math.min(PAGE, matching.length - shown)} more of {matching.length - shown}
        </button>
      )}
    </div>
  );
}

export function ManifestViewer({ artifact }: { artifact: Artifact }) {
  const manifest = useQuery({
    queryKey: ['artifact', artifact.id, artifact.sha256],
    queryFn: async () => {
      const { text, truncated } = await api.artifactText(artifact.download_url, MAX_MANIFEST_BYTES);
      if (truncated) throw new Error('The manifest is too large to show here. Download it instead.');
      return parseRepoManifest(text);
    },
    staleTime: Infinity, // artifacts never change once the job succeeded
  });

  if (manifest.isPending) {
    return (
      <div aria-busy="true" className="stack">
        <Skeleton height={20} width="40%" />
        <Skeleton height={120} />
      </div>
    );
  }
  if (manifest.isError) return <ErrorNotice error={manifest.error} />;
  const m = manifest.data;
  return (
    <div className="manifest">
      <dl className="facts facts-inline">
        <div>
          <dt>Files in archive</dt>
          <dd className="num">{m.totals.files}</dd>
        </div>
        <div>
          <dt>Indexed</dt>
          <dd className="num">{m.totals.included}</dd>
        </div>
        <div>
          <dt>Skipped</dt>
          <dd className="num">{m.totals.skipped}</dd>
        </div>
        <div>
          <dt>Indexed size</dt>
          <dd className="num">{formatBytes(m.totals.included_bytes)}</dd>
        </div>
        <div>
          <dt>Indexed lines</dt>
          <dd className="num">{m.totals.included_lines.toLocaleString()}</dd>
        </div>
      </dl>

      <div className="manifest-grid">
        <div>
          <h3>Languages</h3>
          <table className="table table-compact">
            <thead>
              <tr>
                <th scope="col">Language</th>
                <th scope="col" className="num">Files</th>
                <th scope="col" className="num">Lines</th>
                <th scope="col" className="num">Size</th>
              </tr>
            </thead>
            <tbody>
              {m.languages.map(([language, stats]) => (
                <tr key={language}>
                  <td>
                    <SafeText text={language} />
                  </td>
                  <td className="num">{stats.files}</td>
                  <td className="num">{stats.lines.toLocaleString()}</td>
                  <td className="num">{formatBytes(stats.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <h3>Skip reasons</h3>
          {m.skipReasons.length === 0 ? (
            <p className="muted">No file was skipped.</p>
          ) : (
            <table className="table table-compact">
              <thead>
                <tr>
                  <th scope="col">Reason</th>
                  <th scope="col" className="num">Files</th>
                </tr>
              </thead>
              <tbody>
                {m.skipReasons.map(([reason, count]) => (
                  <tr key={reason}>
                    <td>
                      {SKIP_REASON_LABEL[reason] ?? <SafeText text={reason} />} <code className="mono muted">{reason}</code>
                    </td>
                    <td className="num">{count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <FileList
        title="Indexed files"
        files={m.included}
        columns={['Path', 'Language', 'Lines', 'Size']}
        // Index keys: paths cut to the length limit can repeat.
        render={(file, index) => (
          <tr key={index}>
            <td className="mono path">
              <SafeText text={file.path} />
            </td>
            <td className="num">
              <SafeText text={file.language} />
            </td>
            <td className="num">{file.lines.toLocaleString()}</td>
            <td className="num">{formatBytes(file.bytes)}</td>
          </tr>
        )}
      />
      <FileList
        title="Skipped files"
        files={m.skipped}
        columns={['Path', 'Reason']}
        render={(file, index) => (
          <tr key={index}>
            <td className="mono path">
              <SafeText text={file.path} />
            </td>
            <td className="num nowrap">{SKIP_REASON_LABEL[file.reason] ?? <SafeText text={file.reason} />}</td>
          </tr>
        )}
      />
    </div>
  );
}
