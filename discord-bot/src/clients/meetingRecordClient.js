import { createHttpClient } from './httpClient.js';
import { DocUnavailable } from './docClient.js';

/** Read saved meeting references and acknowledge Discord delivery independently. */
export function createMeetingRecordClient({ baseUrl, apiKey, fetchImpl = fetch }) {
  const { send, parseJson } = createHttpClient({
    baseUrl,
    headers: { 'X-API-Key': apiKey, 'Content-Type': 'application/json' },
    fetchImpl,
    timeoutMs: 10_000,
    networkError: () => new DocUnavailable('meeting record service unavailable'),
    parseError: () => new DocUnavailable('malformed meeting record response'),
  });

  async function request(path, options) {
    const response = await send(path, options);
    if (response.status === 404) return null;
    if (!response.ok) {
      console.warn(`meetingRecordClient: HTTP ${response.status}`);
      throw new DocUnavailable('meeting record service unavailable');
    }
    return parseJson(response);
  }

  return {
    get: (id) => request(`/meeting-records/${encodeURIComponent(id)}`),
    async list(deliveryStatus) {
      const query = new URLSearchParams({
        persistence_status: 'saved',
        delivery_status: deliveryStatus,
      });
      const records = await request(`/meeting-records?${query}`);
      if (!Array.isArray(records)) throw new DocUnavailable('malformed meeting record list');
      return records;
    },
    update: (id, status, messageId) =>
      request(`/meeting-records/${encodeURIComponent(id)}/delivery`, {
        method: 'PATCH',
        body: JSON.stringify({ status, discord_message_id: messageId ?? null }),
      }),
  };
}
