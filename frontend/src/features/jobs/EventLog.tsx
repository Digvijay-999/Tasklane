import { useDeferredValue, useId, useMemo, useState } from 'react';
import { SafeText } from '../../components/SafeText';
import type { JobEvent } from '../../lib/api/types';
import { formatTime } from '../../lib/format';

/** Payload as `key value` pairs, as text. Values are JSON data, never markup or links. */
function payloadText(payload: Record<string, unknown>): string {
  return Object.entries(payload)
    .map(([key, value]) => `${key} ${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join('   ');
}

export function EventLog({ events }: { events: JobEvent[] }) {
  const filterId = useId();
  const progressId = useId();
  const [filter, setFilter] = useState('');
  const [showProgress, setShowProgress] = useState(false);
  const needle = useDeferredValue(filter.trim().toLowerCase());

  const { rows, hiddenProgress } = useMemo(() => {
    let hidden = 0;
    const kept = events.filter((event) => {
      if (event.event_type === 'stage.progress' && !showProgress) {
        hidden++;
        return false;
      }
      if (!needle) return true;
      return `${event.seq} ${event.event_type} ${payloadText(event.payload)}`.toLowerCase().includes(needle);
    });
    return { rows: kept, hiddenProgress: hidden };
  }, [events, needle, showProgress]);

  return (
    <div className="event-log">
      <div className="event-log-controls">
        <label htmlFor={filterId} className="visually-hidden">
          Filter events
        </label>
        <input
          id={filterId}
          type="search"
          className="input input-small"
          placeholder="Filter events, e.g. stage.failed or summarize"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <label className="checkbox" htmlFor={progressId}>
          <input id={progressId} type="checkbox" checked={showProgress} onChange={(e) => setShowProgress(e.target.checked)} />
          Show progress events
        </label>
        <span className="muted num" aria-live="polite">
          {rows.length} of {events.length} events
          {hiddenProgress > 0 && `, ${hiddenProgress} progress events hidden`}
        </span>
      </div>
      {rows.length === 0 ? (
        <div className="empty empty-small">
          <p>{events.length === 0 ? 'No events yet.' : 'No events match this filter.'}</p>
          {filter && (
            <button type="button" className="button" data-variant="ghost" onClick={() => setFilter('')}>
              Clear filter
            </button>
          )}
        </div>
      ) : (
        <div className="table-scroll">
          <table className="table table-compact events-table">
            <caption className="visually-hidden">Stored events of this job, in order</caption>
            <thead>
              <tr>
                <th scope="col">Seq</th>
                <th scope="col">Time</th>
                <th scope="col" className="num">
                  Attempt
                </th>
                <th scope="col">Event</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((event) => (
                <tr key={event.seq} className="event-row" data-type={event.event_type}>
                  <td className="mono">{event.seq}</td>
                  <td className="nowrap">{formatTime(event.created_at)}</td>
                  <td className="num">{event.attempt_no ?? '—'}</td>
                  <td className="mono nowrap">{event.event_type}</td>
                  <td className="mono details">
                    <SafeText text={payloadText(event.payload)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
