import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { JobEvent } from '../api/types';
import { JobEventStream, type StreamDeps, type StreamState, defaultDeps } from './jobEventStream';

/**
 * The job's events, kept live. `onNewEvents` runs for every batch of newly contiguous events.
 * The stream belongs to one job id: render the caller with `key={jobId}` so another job gets a
 * fresh component (and a fresh, empty stream).
 */
export function useJobEvents(
  jobId: string,
  onNewEvents?: (events: JobEvent[]) => void,
  deps: StreamDeps = defaultDeps,
): StreamState & { restart: () => void } {
  const callback = useRef(onNewEvents);
  useEffect(() => {
    callback.current = onNewEvents;
  });
  const [stream] = useState(() => new JobEventStream(jobId, (e) => callback.current?.(e), deps));
  useEffect(() => {
    stream.start();
    return () => stream.stop();
  }, [stream]);
  const state = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  return { ...state, restart: stream.restart };
}
