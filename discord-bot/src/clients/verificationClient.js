import { createHttpClient } from './httpClient.js';

export class VerificationUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = 'VerificationUnavailable';
  }
}

export class RateLimited extends Error {
  constructor(message) {
    super(message);
    this.name = 'RateLimited';
  }
}

export class CodeExpired extends Error {
  constructor(message) {
    super(message);
    this.name = 'CodeExpired';
  }
}

export class TooManyAttempts extends Error {
  constructor(message) {
    super(message);
    this.name = 'TooManyAttempts';
  }
}

export class InvalidCode extends Error {
  constructor(message) {
    super(message);
    this.name = 'InvalidCode';
  }
}

export class NoPendingCode extends Error {
  constructor(message) {
    super(message);
    this.name = 'NoPendingCode';
  }
}

// confirm-code failure statuses → [error class, fallback detail slug].
const CONFIRM_ERRORS = {
  404: [NoPendingCode, 'no_pending_code'],
  410: [CodeExpired, 'expired'],
  429: [TooManyAttempts, 'too_many_attempts'],
  400: [InvalidCode, 'invalid_code'],
};

// Release the undici socket on responses whose body we don't otherwise read
// (unread bodies leak connections under Node's built-in fetch).
async function drain(resp) {
  try {
    await resp.body?.cancel?.();
  } catch {
    /* nothing to release */
  }
}

export function createVerificationClient({
  baseUrl,
  apiKey,
  fetchImpl = fetch,
  timeoutMs = 15000,
}) {
  const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' };
  // Bound each request so /link and /verify-code can't block indefinitely on a
  // hung verification service (mirrors llmClient.js) — the shared helper arms an
  // AbortController when timeoutMs is set.
  const { send, parseJson } = createHttpClient({
    baseUrl,
    headers,
    fetchImpl,
    timeoutMs,
    networkError: () => new VerificationUnavailable('network error reaching verification service'),
    parseError: () => new VerificationUnavailable('malformed verification response'),
  });

  return {
    async requestCode({ subject, email }) {
      const resp = await send('/verification/request-code', {
        method: 'POST',
        body: JSON.stringify({ subject, email }),
      });
      if (resp.status === 202) {
        await drain(resp);
        return;
      }
      if (resp.status === 429) {
        const body = await resp.json().catch(() => ({}));
        throw new RateLimited(body.detail ?? 'rate_limited');
      }
      await drain(resp);
      throw new VerificationUnavailable(`verification service returned ${resp.status}`);
    },

    async confirmCode({ subject, code }) {
      const resp = await send('/verification/confirm-code', {
        method: 'POST',
        body: JSON.stringify({ subject, code }),
      });
      if (resp.status === 200) return parseJson(resp);
      const mapping = CONFIRM_ERRORS[resp.status];
      if (mapping) {
        const [ErrorClass, fallback] = mapping;
        const body = await resp.json().catch(() => ({}));
        throw new ErrorClass(body.detail ?? fallback);
      }
      await drain(resp);
      throw new VerificationUnavailable(`verification service returned ${resp.status}`);
    },
  };
}
