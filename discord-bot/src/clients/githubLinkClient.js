import { createHttpClient } from './httpClient.js';

export function createGithubLinkClient({ baseUrl, apiKey, fetchImpl }) {
  if (!baseUrl || !apiKey) return null;
  const http = createHttpClient({
    baseUrl,
    headers: { 'X-API-Key': apiKey, 'Content-Type': 'application/json' },
    fetchImpl,
    timeoutMs: 20000,
    networkError: () => new Error('GitHub linking unavailable'),
    parseError: () => new Error('GitHub linking unavailable'),
  });
  return {
    async request(action, personId, discordId, confirmationCode) {
      if (!['start', 'confirm', 'status', 'unlink'].includes(action))
        throw new Error('Invalid link action');
      const response = await http.send(`/github/oauth/${action}`, {
        method: 'POST',
        body: JSON.stringify({
          person_id: personId,
          discord_id: discordId,
          ...(action === 'confirm' ? { confirmation_code: confirmationCode } : {}),
        }),
      });
      if (!response.ok) throw new Error('GitHub linking unavailable; check status or reconnect');
      const result = await http.parseJson(response);
      if (action === 'start') {
        const url = new URL(result.authorize_url);
        if (
          url.origin !== 'https://github.com' ||
          url.pathname !== '/login/oauth/authorize' ||
          url.username ||
          url.password
        ) {
          throw new Error('GitHub linking unavailable');
        }
      }
      return result;
    },
  };
}
