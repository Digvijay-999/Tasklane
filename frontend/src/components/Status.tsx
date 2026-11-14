import type { JobStatus } from '../lib/api/types';
import { STATUS_LABEL } from '../lib/labels';

/**
 * One glyph per job state. The *shape* carries the meaning (DESIGN.md §3.1), so a state reads
 * without color: dashed ring (queued), half-filled (running), circular arrow (waiting to retry),
 * ring with a square (cancelling), check (succeeded), cross (failed), filled square (cancelled).
 */
export function StatusGlyph({ status, size = 14 }: { status: string; size?: number }) {
  const common = { width: size, height: size, viewBox: '0 0 16 16', 'aria-hidden': true, focusable: false } as const;
  switch (status) {
    case 'queued':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeDasharray="2.4 2.2" />
        </svg>
      );
    case 'running':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M8 3.6a4.4 4.4 0 0 1 0 8.8z" fill="currentColor" />
        </svg>
      );
    case 'retry_wait':
      return (
        <svg {...common}>
          <path d="M13.2 8A5.2 5.2 0 1 1 11.6 4.3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <path d="M9.6 1.9l2.4 2.5-3 1.4z" fill="currentColor" />
        </svg>
      );
    case 'cancel_requested':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <rect x="5.6" y="5.6" width="4.8" height="4.8" rx="0.6" fill="currentColor" />
        </svg>
      );
    case 'succeeded':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M4.8 8.2l2.1 2.1 4.3-4.5" fill="none" stroke="var(--bg)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case 'failed':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="var(--bg)" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      );
    case 'cancelled':
      return (
        <svg {...common}>
          <rect x="2" y="2" width="12" height="12" rx="2" fill="currentColor" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="2" fill="currentColor" />
        </svg>
      );
  }
}

/** Glyph + label in the state's color. Never color alone. */
export function StatusBadge({ status, label }: { status: JobStatus | string; label?: string }) {
  return (
    <span className="status" data-status={status}>
      <StatusGlyph status={status} />
      <span>{label ?? STATUS_LABEL[status as JobStatus] ?? status}</span>
    </span>
  );
}
