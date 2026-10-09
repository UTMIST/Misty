export class LlmUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = 'LlmUnavailable';
  }
}

// HTTP client for the internal LLM service (POST /chat). Mirrors the shape of
// directoryClient.js: injectable fetchImpl, X-API-Key auth, a single typed
// error. The service's 429/504/502/401 all surface the same way to the bot
// ("try again shortly"), so they collapse into one LlmUnavailable.
export function createLlmClient({ baseUrl, apiKey, fetchImpl = fetch, timeoutMs = 120000 }) {
  const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' };

  return {
    async chat({ messages, system, model, maxTokens = 1024 }) {
      const body = { messages, max_tokens: maxTokens };
      if (system) body.system = system;
      if (model) body.model = model;

      let resp;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        resp = await fetchImpl(`${baseUrl}/chat`, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: controller.signal,
        });
      } catch {
        throw new LlmUnavailable('network error reaching llm service');
      } finally {
        clearTimeout(timer);
      }
      if (!resp.ok) throw new LlmUnavailable(`llm service returned ${resp.status}`);
      let data;
      try {
        data = await resp.json();
      } catch {
        throw new LlmUnavailable('malformed llm response');
      }
      return { content: data.content, model: data.model, usage: data.usage };
    },
  };
}
