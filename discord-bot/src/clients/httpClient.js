import { withSpan, traceHeaders } from '../telemetry.js';
// Shared HTTP wrapper for the internal service clients (directory/doc/
// verification). Each client injects its own base URL, auth headers, and
// error factories so its specific "unavailable" error type and message survive
// — the helper never collapses them into a single generic error. An optional
// timeout arms an AbortController (used by clients that must not block on a hung
// service); when timeoutMs is omitted no signal is attached, matching the
// untimed clients' original behavior.
export function createHttpClient({
  baseUrl,
  headers,
  fetchImpl = fetch,
  timeoutMs,
  networkError = () => new Error('network error'),
  parseError = () => new Error('malformed response'),
}) {
  async function send(path, options = {}) {
    const { headers: perCall, ...rest } = options;
    const mergedHeaders = perCall ? { ...headers, ...perCall } : headers;

    let controller;
    let timer;
    if (timeoutMs !== undefined) {
      controller = new AbortController();
      timer = setTimeout(() => controller.abort(), timeoutMs);
    }
    try {
      return await withSpan('http.client', async (span) => {
        const response = await fetchImpl(`${baseUrl}${path}`, {
          ...rest,
          headers: span.traceparent ? { ...mergedHeaders, ...traceHeaders() } : mergedHeaders,
          redirect: 'error',
          ...(controller ? { signal: controller.signal } : {}),
        });
        if (response.status >= 400)
          span.outcome(
            response.status === 429
              ? 'rate_limited'
              : [401, 403].includes(response.status)
                ? 'denied'
                : 'failed',
          );
        return response;
      });
    } catch {
      throw networkError();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function parseJson(resp) {
    try {
      return await resp.json();
    } catch {
      throw parseError();
    }
  }

  return { send, parseJson };
}
