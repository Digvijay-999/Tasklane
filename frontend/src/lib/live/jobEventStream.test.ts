import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EventPage, JobEvent } from '../api/types';
import { JobEventStream, POLL_INTERVAL_MS, type StreamDeps } from './jobEventStream';
import { useJobEvents } from './useJobEvents';

/** A stand-in for the browser's EventSource that the test drives by hand. */
class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((e: MessageEvent) => void)[]>();
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (e: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  open() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }
  send(event: JobEvent) {
    this.dispatch(event.event_type, JSON.stringify(event));
  }
  end() {
    this.dispatch('stream.end', JSON.stringify({ reason: 'job_finished', status: 'succeeded' }));
  }
  drop(closed: boolean) {
    this.readyState = closed ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.onerror?.();
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
  private dispatch(type: string, data: string) {
    this.listeners.get(type)?.forEach((listener) => listener(new MessageEvent(type, { data })));
  }
}

const latest = () => FakeEventSource.instances[FakeEventSource.instances.length - 1]!;

function ev(seq: number, event_type = 'stage.progress', payload: Record<string, unknown> = {}): JobEvent {
  return { seq, event_type, attempt_no: 1, created_at: `2026-11-14T12:00:${String(seq).padStart(2, '0')}Z`, payload };
}

/** History route over a mutable list of stored events, paged like the server (limit 500). */
function historyOf(stored: JobEvent[]) {
  return vi.fn(async (_job: string, after: number): Promise<EventPage> => {
    const items = stored.filter((e) => e.seq > after);
    return { items, next_after: null, last_seq: stored.length ? stored[stored.length - 1]!.seq : 0 };
  });
}

function deps(stored: JobEvent[], probe: StreamDeps['probe'] = vi.fn(async () => 'unreachable' as const)): StreamDeps {
  return { fetchPage: historyOf(stored), probe, createSource: (url) => new FakeEventSource(url) as unknown as EventSource };
}

const seqs = (stream: JobEventStream) => stream.getSnapshot().events.map((e) => e.seq);
const flush = () => act(async () => {});

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('JobEventStream', () => {
  it('loads the history first, then opens the stream after the last seq it has', async () => {
    const stream = new JobEventStream('job-1', undefined, deps([ev(1, 'job.created'), ev(2), ev(3)]));
    stream.start();
    await flush();
    expect(seqs(stream)).toEqual([1, 2, 3]);
    expect(latest().url).toBe('/v1/jobs/job-1/events?after=3');
    expect(stream.getSnapshot().mode).toBe('connecting');
    latest().open();
    expect(stream.getSnapshot().mode).toBe('live');
    stream.stop();
  });

  it('ignores replayed duplicates and appends new events in order', async () => {
    const onNew = vi.fn();
    const stream = new JobEventStream('job-1', onNew, deps([ev(1), ev(2)]));
    stream.start();
    await flush();
    latest().open();
    latest().send(ev(2)); // replayed by the server after a reconnect
    latest().send(ev(3));
    expect(seqs(stream)).toEqual([1, 2, 3]);
    expect(onNew).toHaveBeenLastCalledWith([ev(3)]);
    stream.stop();
  });

  it('closes on stream.end and never reconnects for a finished job', async () => {
    const stream = new JobEventStream('job-1', undefined, deps([ev(1)]));
    stream.start();
    await flush();
    const source = latest();
    source.open();
    source.end();
    expect(source.readyState).toBe(FakeEventSource.CLOSED);
    expect(stream.getSnapshot().mode).toBe('ended');
    source.drop(true); // a late error from the closed source changes nothing
    await flush();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(stream.getSnapshot().mode).toBe('ended');
  });

  it('fills a gap (7 right after 5) from the history route and shows no hole meanwhile', async () => {
    const stored = [1, 2, 3, 4, 5].map((n) => ev(n));
    const d = deps(stored);
    const stream = new JobEventStream('job-1', undefined, d);
    stream.start();
    await flush();
    latest().open();
    stored.push(ev(6), ev(7));
    latest().send(ev(7)); // 6 never arrived on the stream
    expect(seqs(stream)).toEqual([1, 2, 3, 4, 5]); // 7 is held back until 6 is known
    await flush();
    expect(d.fetchPage).toHaveBeenLastCalledWith('job-1', 5, expect.any(AbortSignal));
    expect(seqs(stream)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    stream.stop();
  });

  it('lets the browser reconnect a dropped connection by itself', async () => {
    const stream = new JobEventStream('job-1', undefined, deps([ev(1)]));
    stream.start();
    await flush();
    latest().open();
    latest().drop(false); // readyState CONNECTING: EventSource retries with Last-Event-ID
    expect(stream.getSnapshot().mode).toBe('reconnecting');
    expect(FakeEventSource.instances).toHaveLength(1);
    latest().open();
    expect(stream.getSnapshot().mode).toBe('live');
    stream.stop();
  });

  it('reconnects itself from the last seq when the browser gives up (API down)', async () => {
    vi.useFakeTimers();
    const stored = [ev(1), ev(2)];
    const stream = new JobEventStream('job-1', undefined, deps(stored));
    stream.start();
    await vi.advanceTimersByTimeAsync(0);
    latest().open();
    latest().send(ev(3));
    latest().drop(true); // CLOSED; the probe says "unreachable"
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.getSnapshot().mode).toBe('reconnecting');
    await vi.advanceTimersByTimeAsync(1000);
    expect(latest().url).toBe('/v1/jobs/job-1/events?after=3');
    stream.stop();
  });

  it('falls back to polling the history on 503 stream_limit_reached, and ends when the job finishes', async () => {
    vi.useFakeTimers();
    const stored = [ev(1, 'job.created')];
    const d = deps(stored, vi.fn(async () => 'limit' as const));
    const stream = new JobEventStream('job-1', undefined, d);
    stream.start();
    await vi.advanceTimersByTimeAsync(0);
    latest().drop(true); // the server answered 503: EventSource does not retry that
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.getSnapshot().mode).toBe('polling');

    stored.push(ev(2, 'job.status_changed', { from: 'queued', to: 'running' }));
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(seqs(stream)).toEqual([1, 2]);
    expect(stream.getSnapshot().mode).toBe('polling');

    stored.push(ev(3, 'job.status_changed', { from: 'running', to: 'succeeded' }));
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(seqs(stream)).toEqual([1, 2, 3]);
    expect(stream.getSnapshot().mode).toBe('ended');
    const calls = (d.fetchPage as ReturnType<typeof vi.fn>).mock.calls.length;
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    expect((d.fetchPage as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls); // polling stopped
    stream.stop();
  });

  it('tries the stream again while polling and stops polling once it is back', async () => {
    vi.useFakeTimers();
    const d = deps([ev(1)], vi.fn(async () => 'limit' as const));
    const stream = new JobEventStream('job-1', undefined, d);
    stream.start();
    await vi.advanceTimersByTimeAsync(0);
    latest().drop(true);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(FakeEventSource.instances).toHaveLength(2);
    latest().open();
    expect(stream.getSnapshot().mode).toBe('live');
    const calls = (d.fetchPage as ReturnType<typeof vi.fn>).mock.calls.length;
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect((d.fetchPage as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
    stream.stop();
  });

  it('opens nothing after stop(), even with a reconnect and a gap fill pending', async () => {
    vi.useFakeTimers();
    const stored = [ev(1), ev(2)];
    const d = deps(stored);
    const stream = new JobEventStream('job-1', undefined, d);
    stream.start();
    await vi.advanceTimersByTimeAsync(0);
    latest().open();
    latest().send(ev(4)); // gap: 3 is not in the history yet, so a fill retry gets scheduled
    latest().drop(true); // and a reconnect with backoff
    await vi.advanceTimersByTimeAsync(0);
    const before = FakeEventSource.instances.length;
    stream.stop(); // unmount
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.instances).toHaveLength(before);
  });

  it('still fills gaps after a stop() that interrupted a fill (React StrictMode remount)', async () => {
    const stored = [ev(1), ev(2)];
    const d = deps(stored);
    let release: () => void = () => {};
    const slow = d.fetchPage as ReturnType<typeof vi.fn>;
    const stream = new JobEventStream('job-1', undefined, d);
    stream.start();
    await flush();
    latest().open();
    slow.mockImplementationOnce(() => new Promise<EventPage>((resolve) => (release = () => resolve({ items: [], next_after: null, last_seq: 2 }))));
    latest().send(ev(4)); // starts a fill that hangs
    stream.stop();
    release();
    await flush();
    stored.push(ev(3), ev(4));
    stream.start(); // loads 3 and 4 from the history
    await flush();
    latest().open();
    stored.push(ev(5), ev(6)); // committed (the server stores before it streams)
    latest().send(ev(6)); // 5 was missed: a new gap must be filled again
    await flush();
    expect(seqs(stream)).toEqual([1, 2, 3, 4, 5, 6]);
    stream.stop();
  });

  it('reports a deleted job', async () => {
    const stream = new JobEventStream('job-1', undefined, deps([ev(1)], vi.fn(async () => 'not_found' as const)));
    stream.start();
    await flush();
    latest().drop(true);
    await flush();
    expect(stream.getSnapshot().mode).toBe('not_found');
  });
});

describe('useJobEvents', () => {
  it('resumes after a page reload from the reloaded history, without duplicates', async () => {
    const stored = [1, 2, 3, 4].map((n) => ev(n));
    // First "page load".
    const first = renderHook(() => useJobEvents('job-1', undefined, deps(stored)));
    await waitFor(() => expect(first.result.current.lastSeq).toBe(4));
    latest().open();
    act(() => latest().send(ev(5)));
    stored.push(ev(5));
    first.unmount(); // reload: EventSource and its Last-Event-ID are gone

    stored.push(ev(6));
    const second = renderHook(() => useJobEvents('job-1', undefined, deps(stored)));
    await waitFor(() => expect(second.result.current.lastSeq).toBe(6));
    expect(latest().url).toBe('/v1/jobs/job-1/events?after=6');
    act(() => {
      latest().open();
      latest().send(ev(6)); // a replay of something the history already had
      latest().send(ev(7));
    });
    expect(second.result.current.events.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    second.unmount();
  });
});
