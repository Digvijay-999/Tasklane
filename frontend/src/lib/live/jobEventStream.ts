import { api } from '../api/client';
import { isTerminal, type EventPage, type JobEvent } from '../api/types';

/**
 * Follows one job's events: full history first (GET .../events/history), then the live SSE
 * stream from the last seq we hold (GET .../events?after=<seq>). See docs/api.md, "Events".
 *
 * - Every event is kept in a Map by seq, so replays and duplicates merge harmlessly.
 * - `lastSeq` is the highest seq with *every* event 1..lastSeq present. Only that contiguous
 *   prefix is shown, so the timeline never draws from a list with a hole in it.
 * - A gap (seq 7 arriving when we hold up to 5) is filled from the history route.
 * - `stream.end` closes the EventSource for good (it would otherwise reconnect every few s).
 * - A connection the browser gave up on (readyState CLOSED: the server answered 503/404, or the
 *   API is down) is probed with fetch to learn why: stream limit -> poll the history route every
 *   2 s and retry the stream every 30 s; job deleted -> stop; anything else -> reconnect with
 *   backoff, resuming from `lastSeq` (?after=, since EventSource can't send headers itself).
 */

export type StreamMode =
  | 'loading' // reading history
  | 'connecting'
  | 'live'
  | 'reconnecting' // the connection dropped; resuming from lastSeq
  | 'polling' // the server's stream limit is reached: reading the history route instead
  | 'ended' // job finished and every event received
  | 'not_found'
  | 'error'; // history couldn't be loaded

export interface StreamState {
  events: JobEvent[]; // contiguous, ordered by seq
  lastSeq: number;
  mode: StreamMode;
  reconnects: number;
  missing: number; // seqs the history route doesn't have (deleted); never silently skipped
  error: unknown;
}

// Events are named, so each type needs its own listener (onmessage never fires). A type missing
// from this list is still caught: the next known event shows a gap, which the history fills.
export const STREAM_EVENT_TYPES = [
  'job.created',
  'job.status_changed',
  'job.cancel_superseded',
  'job.requeued',
  'job.manual_retry',
  'attempt.started',
  'attempt.lease_expired',
  'stage.started',
  'stage.progress',
  'stage.completed',
  'stage.failed',
  'stage.skipped',
] as const;

export type ProbeResult = 'ok' | 'limit' | 'not_found' | 'unreachable';

export interface StreamDeps {
  fetchPage: (jobId: string, after: number, signal: AbortSignal) => Promise<EventPage>;
  probe: (url: string) => Promise<ProbeResult>;
  createSource: (url: string) => EventSource;
}

export const POLL_INTERVAL_MS = 2000;
export const STREAM_RETRY_WHILE_POLLING_MS = 30_000;
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15_000];

async function probeStream(url: string): Promise<ProbeResult> {
  const controller = new AbortController();
  try {
    const response = await fetch(url, { signal: controller.signal, headers: { Accept: 'text/event-stream' } });
    if (response.ok && (response.headers.get('content-type') ?? '').startsWith('text/event-stream')) {
      controller.abort(); // the stream works again; this probe must not hold a server slot
      return 'ok';
    }
    if (response.ok) {
      controller.abort();
      return 'unreachable'; // something answered, but not the event stream (a proxy page, say)
    }
    let code: unknown;
    try {
      code = ((await response.json()) as { error?: { code?: string } }).error?.code;
    } catch {
      code = undefined;
    }
    if (response.status === 503 && code === 'stream_limit_reached') return 'limit';
    if (response.status === 404 && code === 'job_not_found') return 'not_found';
    return 'unreachable';
  } catch {
    return 'unreachable';
  }
}

export const defaultDeps: StreamDeps = {
  fetchPage: (jobId, after, signal) => api.events(jobId, after, 500, signal),
  probe: probeStream,
  createSource: (url) => new EventSource(url),
};

export class JobEventStream {
  private readonly byseq = new Map<number, JobEvent>();
  private lastSeq = 0;
  private status: string | null = null; // the job status according to the events
  private source: EventSource | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private fillTimer: ReturnType<typeof setTimeout> | undefined;
  private pollTimer: ReturnType<typeof setInterval> | undefined;
  private filling = false;
  private polling = false;
  private backoffStep = 0;
  private generation = 0; // bumped by stop(); work started before it is ignored
  private controller = new AbortController();
  private listeners = new Set<() => void>();
  private state: StreamState = { events: [], lastSeq: 0, mode: 'loading', reconnects: 0, missing: 0, error: null };

  constructor(
    private readonly jobId: string,
    private readonly onNewEvents: (events: JobEvent[]) => void = () => {},
    private readonly deps: StreamDeps = defaultDeps,
  ) {}

  // --- React glue (useSyncExternalStore) ---------------------------------------------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): StreamState => this.state;

  private set(patch: Partial<StreamState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  // --- Lifecycle -----------------------------------------------------------------------------------

  start(): void {
    this.stop();
    const generation = ++this.generation;
    this.controller = new AbortController();
    this.set({ mode: 'loading', error: null });
    this.loadFrom(this.lastSeq)
      .then(() => {
        if (generation === this.generation) this.open();
      })
      .catch((error: unknown) => {
        if (generation !== this.generation || isAbort(error)) return;
        this.set({ mode: 'error', error });
      });
  }

  stop(): void {
    this.generation++;
    this.controller.abort();
    this.closeSource();
    clearTimeout(this.reconnectTimer);
    clearTimeout(this.fillTimer);
    this.stopPolling();
    this.filling = false; // a fill cut off by stop() must not block the next start()
  }

  /** Follow the job again after it was reopened (manual retry) or after an error. */
  restart = (): void => {
    if (this.state.mode === 'error' || this.state.mode === 'loading') this.start();
    else this.open();
  };

  // --- History -------------------------------------------------------------------------------------

  /** Read every page after `after`; returns the job's last seq as of the final page. */
  private async loadFrom(after: number): Promise<number> {
    const generation = this.generation;
    for (;;) {
      const page = await this.deps.fetchPage(this.jobId, after, this.controller.signal);
      if (generation !== this.generation) return page.last_seq;
      const first = page.items[0];
      if (first && first.seq > this.lastSeq + 1 && after === this.lastSeq) {
        // The history itself lacks these seqs (deleted). Count them; don't wait forever.
        this.set({ missing: this.state.missing + (first.seq - this.lastSeq - 1) });
        this.lastSeq = first.seq - 1;
      }
      this.add(page.items);
      if (page.next_after === null) return page.last_seq;
      after = page.next_after;
    }
  }

  private add(items: JobEvent[]): void {
    for (const event of items) {
      if (event.seq > this.lastSeq && !this.byseq.has(event.seq)) this.byseq.set(event.seq, event);
    }
    const fresh: JobEvent[] = [];
    let next = this.byseq.get(this.lastSeq + 1);
    while (next) {
      fresh.push(next);
      this.lastSeq = next.seq;
      if (next.event_type === 'job.status_changed' && typeof next.payload.to === 'string') {
        this.status = next.payload.to;
      }
      next = this.byseq.get(this.lastSeq + 1);
    }
    if (fresh.length === 0) return;
    this.set({ events: [...this.state.events, ...fresh], lastSeq: this.lastSeq });
    this.onNewEvents(fresh);
  }

  private get hasGap(): boolean {
    for (const seq of this.byseq.keys()) if (seq > this.lastSeq) return true;
    return false;
  }

  private fillGap(): void {
    if (this.filling) return;
    this.filling = true;
    const generation = this.generation;
    this.loadFrom(this.lastSeq)
      .catch(() => undefined) // the next event (or poll) tries again
      .finally(() => {
        if (generation !== this.generation) return;
        this.filling = false;
        if (this.hasGap) {
          // Still missing after reading the history up to the job's last seq: the stream is
          // ahead of what we could read (a race). Try once more shortly.
          this.fillTimer = setTimeout(() => this.fillGap(), 500);
        }
      });
  }

  // --- Live stream ---------------------------------------------------------------------------------

  /** Schedule open() for this generation only: a timer that outlives stop() does nothing. */
  private reopenAfter(delay: number): void {
    clearTimeout(this.reconnectTimer);
    const generation = this.generation;
    this.reconnectTimer = setTimeout(() => {
      if (generation === this.generation) this.open();
    }, delay);
  }

  private open(): void {
    this.closeSource();
    clearTimeout(this.reconnectTimer);
    const source = this.deps.createSource(
      `/v1/jobs/${encodeURIComponent(this.jobId)}/events?after=${this.lastSeq}`,
    );
    this.source = source;
    if (!this.polling) this.set({ mode: this.state.mode === 'reconnecting' ? 'reconnecting' : 'connecting' });

    source.onopen = () => {
      if (this.source !== source) return;
      this.backoffStep = 0;
      this.stopPolling();
      this.set({ mode: 'live' });
    };
    const onMessage = (message: MessageEvent<string>) => {
      if (this.source !== source) return;
      let event: JobEvent;
      try {
        event = JSON.parse(message.data) as JobEvent;
      } catch {
        return;
      }
      if (typeof event.seq !== 'number' || event.seq <= this.lastSeq) return; // replayed duplicate
      this.add([event]);
      if (this.hasGap) this.fillGap();
    };
    STREAM_EVENT_TYPES.forEach((type) => source.addEventListener(type, onMessage as EventListener));
    source.addEventListener('stream.end', () => {
      if (this.source !== source) return;
      this.closeSource(); // finished: don't let EventSource reconnect forever
      this.stopPolling();
      if (this.hasGap) this.fillGap();
      this.set({ mode: 'ended' });
    });
    source.onerror = () => {
      if (this.source !== source) return;
      if (source.readyState === EventSource.CLOSED) {
        void this.handleClosed();
      } else {
        // CONNECTING: the browser reconnects by itself and sends Last-Event-ID.
        this.set({ mode: this.polling ? 'polling' : 'reconnecting', reconnects: this.state.reconnects + 1 });
      }
    };
  }

  private async handleClosed(): Promise<void> {
    this.closeSource();
    const generation = this.generation;
    const url = `/v1/jobs/${encodeURIComponent(this.jobId)}/events?after=${this.lastSeq}`;
    const result = await this.deps.probe(url);
    if (generation !== this.generation) return;
    if (result === 'limit') {
      this.startPolling();
      this.reopenAfter(STREAM_RETRY_WHILE_POLLING_MS);
    } else if (result === 'not_found') {
      this.stopPolling();
      this.set({ mode: 'not_found' });
    } else {
      // 'ok' too goes through the backoff: if EventSource keeps failing while a plain fetch
      // works, reconnecting at once would loop without pause. (onopen resets the backoff.)
      const delay = BACKOFF_MS[Math.min(this.backoffStep++, BACKOFF_MS.length - 1)]!;
      this.set({ mode: this.polling ? 'polling' : 'reconnecting', reconnects: this.state.reconnects + 1 });
      this.reopenAfter(delay);
    }
  }

  private startPolling(): void {
    this.set({ mode: 'polling' });
    if (this.polling) return;
    this.polling = true;
    const generation = this.generation;
    const poll = () => {
      this.loadFrom(this.lastSeq)
        .then((lastSeq) => {
          if (generation !== this.generation || !this.polling) return;
          if (this.status !== null && isTerminal(this.status) && this.lastSeq >= lastSeq) {
            this.stopPolling();
            clearTimeout(this.reconnectTimer);
            this.set({ mode: 'ended' });
          }
        })
        .catch(() => undefined); // keep polling; the API may be back on the next tick
    };
    poll();
    this.pollTimer = setInterval(poll, POLL_INTERVAL_MS);
  }

  private stopPolling(): void {
    this.polling = false;
    clearInterval(this.pollTimer);
  }

  private closeSource(): void {
    this.source?.close();
    this.source = null;
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
