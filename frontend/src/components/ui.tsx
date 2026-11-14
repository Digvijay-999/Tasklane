import { AlertDialog } from '@base-ui/react/alert-dialog';
import type { ReactNode } from 'react';
import { ApiError } from '../lib/api/client';
import { formatTime } from '../lib/format';

export function ProgressBar({ value, label, tone }: { value: number; label: string; tone?: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <span
      className="progress"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped)}
      data-tone={tone}
    >
      <span className="progress-fill" style={{ transform: `scaleX(${clamped / 100})` }} />
    </span>
  );
}

/** A placeholder block with the final content's dimensions (no spinners). */
export function Skeleton({ width = '100%', height = 14 }: { width?: number | string; height?: number }) {
  return <span className="skeleton" aria-hidden="true" style={{ width, height }} />;
}

export function SkeletonRows({ rows, columns }: { rows: number; columns: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, r) => (
        <tr key={r} aria-hidden="true">
          {Array.from({ length: columns }, (_, c) => (
            <td key={c}>
              <Skeleton width={c === 1 ? '70%' : '50%'} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

/** The server's safe message, with its code, next to the action that failed. */
export function ErrorNotice({ error, action }: { error: unknown; action?: ReactNode }) {
  const apiError = error instanceof ApiError ? error : null;
  return (
    <div className="notice" data-tone="failed" role="alert">
      <p>
        {apiError?.message ?? 'Something went wrong.'}{' '}
        {apiError && !apiError.unreachable && <code className="mono">{apiError.code}</code>}
      </p>
      {action}
    </div>
  );
}

/** Marks data that is no longer current because the last refresh failed. */
export function StaleNote({ error, updatedAt }: { error: unknown; updatedAt: number }) {
  if (!error || !updatedAt) return null;
  const why = error instanceof ApiError && error.unreachable ? "can't reach the API" : 'the last refresh failed';
  return (
    <p className="stale-note" role="status">
      Not current: last updated {formatTime(new Date(updatedAt).toISOString())} ({why}). Retrying.
    </p>
  );
}

interface ConfirmProps {
  trigger: ReactNode;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
  describedBy?: string;
}

/** Base UI AlertDialog: focus trap, Escape, focus return and inert background are the library's. */
export function ConfirmDialog({ trigger, title, description, confirmLabel, cancelLabel, onConfirm, disabled, describedBy }: ConfirmProps) {
  return (
    <AlertDialog.Root>
      <AlertDialog.Trigger className="button" data-variant="secondary" disabled={disabled} aria-describedby={describedBy}>
        {trigger}
      </AlertDialog.Trigger>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="dialog-backdrop" />
        <AlertDialog.Popup className="dialog">
          <AlertDialog.Title className="dialog-title">{title}</AlertDialog.Title>
          <AlertDialog.Description className="dialog-description">{description}</AlertDialog.Description>
          <div className="dialog-actions">
            <AlertDialog.Close className="button" data-variant="secondary">
              {cancelLabel}
            </AlertDialog.Close>
            <AlertDialog.Close className="button" data-variant="primary" onClick={onConfirm}>
              {confirmLabel}
            </AlertDialog.Close>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
