// Display formatting. Binary units (KiB, MiB) match the server's limits (50 MiB = 52428800 bytes).

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1).replace(/\.0$/, '')} ${units[unit]}`;
}

/** 0.7 s, 12 s, 3 min 4 s, 2 h 5 min. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.round(Math.max(0, ms))} ms`;
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${Math.round(seconds % 60)} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

export function durationBetween(from: string | null, to: string | null, now = Date.now()): number | null {
  if (!from) return null;
  return Math.max(0, (to ? Date.parse(to) : now) - Date.parse(from));
}

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const dateTimeFormat = new Intl.DateTimeFormat(undefined, {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
});

export const formatTime = (iso: string | null | undefined): string => (iso ? timeFormat.format(new Date(iso)) : '—');
export const formatDateTime = (iso: string | null | undefined): string =>
  iso ? dateTimeFormat.format(new Date(iso)) : '—';

export const shortId = (id: string): string => id.slice(0, 8);

export const percent = (value: number): string => `${Math.round(value)}%`;
