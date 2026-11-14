import type {
  ErrorBody,
  EventPage,
  Job,
  JobDetail,
  JobList,
  JobStatus,
  Meta,
  Overview,
  Ready,
  WorkflowType,
} from './types';

/** An API failure with the server's safe message and stable code, or a network failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  /** True when no JSON answer came back at all: the API (or the proxy in front of it) is down. */
  readonly unreachable: boolean;

  constructor(status: number, body: ErrorBody['error'] | null) {
    super(body?.message ?? "Can't reach the Tasklane API. Check that the stack is running.");
    this.status = status;
    this.code = body?.code ?? 'api_unreachable';
    this.details = body?.details ?? {};
    this.requestId = body?.request_id ?? null;
    this.unreachable = body === null;
  }
}

async function readError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as ErrorBody;
    if (body && typeof body.error?.code === 'string') return new ApiError(response.status, body.error);
  } catch {
    // not JSON: the dev proxy answers 5xx with an empty body when the API is down
  }
  return new ApiError(response.status, null);
}

export async function send(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers: { Accept: 'application/json', ...init?.headers } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, null);
  }
  if (!response.ok) throw await readError(response);
  return response;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return (await send(path, init)).json() as Promise<T>;
}

export interface JobFilters {
  status: JobStatus[];
  workflow: WorkflowType[];
}

export interface CreateJobResult {
  job: Job;
  replayed: boolean;
}

export const api = {
  ready: async (): Promise<Ready> => {
    // 503 still carries a JSON body with the checks; only a missing body means "unreachable".
    let response: Response;
    try {
      response = await fetch('/health/ready', { headers: { Accept: 'application/json' } });
    } catch {
      throw new ApiError(0, null);
    }
    try {
      return (await response.json()) as Ready;
    } catch {
      throw new ApiError(response.status, null);
    }
  },
  meta: () => json<Meta>('/v1/meta'),
  overview: () => json<Overview>('/v1/overview'),
  listJobs: (filters: JobFilters, cursor: string | null, limit = 25) => {
    const query = new URLSearchParams();
    filters.status.forEach((s) => query.append('status', s));
    filters.workflow.forEach((w) => query.append('workflow_type', w));
    query.set('limit', String(limit));
    if (cursor) query.set('cursor', cursor);
    return json<JobList>(`/v1/jobs?${query}`);
  },
  job: (id: string) => json<JobDetail>(`/v1/jobs/${encodeURIComponent(id)}`),
  events: (id: string, after: number, limit = 500, signal?: AbortSignal) =>
    json<EventPage>(`/v1/jobs/${encodeURIComponent(id)}/events/history?after=${after}&limit=${limit}`, {
      signal,
    }),
  cancel: (id: string) => json<JobDetail>(`/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  retry: (id: string) => json<JobDetail>(`/v1/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  createJob: async (
    body: { workflow_type: WorkflowType; upload_id: string; options?: Record<string, unknown> },
    idempotencyKey: string,
  ): Promise<CreateJobResult> => {
    const response = await send('/v1/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(body),
    });
    return {
      job: (await response.json()) as Job,
      replayed: response.headers.get('Idempotent-Replayed') === 'true',
    };
  },
  /** An artifact's text (summary preview, manifest), capped so a huge file can't freeze the tab. */
  artifactText: async (url: string, maxBytes: number): Promise<{ text: string; truncated: boolean }> => {
    const response = await send(url, { headers: { Accept: '*/*' } });
    const buffer = await response.arrayBuffer();
    const truncated = buffer.byteLength > maxBytes;
    const text = new TextDecoder('utf-8').decode(truncated ? buffer.slice(0, maxBytes) : buffer);
    return { text, truncated };
  },
};
