const TRACE_ID = /^[0-9a-f]{32}$/;

/** The "Open trace" link: the server's template (TRACE_VIEWER_URL) with {trace_id} filled in.
 * Null when no viewer is configured, the id is malformed, or the result isn't an http(s) URL. */
export function traceUrl(template: string | null | undefined, traceId: string): string | null {
  if (!template || !TRACE_ID.test(traceId)) return null;
  const url = template.replaceAll('{trace_id}', traceId);
  try {
    const { protocol } = new URL(url);
    return protocol === 'http:' || protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}
