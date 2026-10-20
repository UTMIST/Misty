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
//
// `chat` serves both the text-only path (plain-string messages, used by the
// helper transcript) and the client-executed tool path (#70/#71): pass `tools`
// and structured content-block messages, then read back `stop_reason` and the
// assistant `content_blocks`. The bot never interprets reasoning/continuation
// blocks — it replays the returned `content_blocks` verbatim on the next turn —
// so this client stays agnostic to which provider (Bedrock reasoning vs OpenAI
// reasoning_state) the service runs. Text-only responses omit `content_blocks`
// (response_model_exclude_none), so the existing `{ content }` callers are
// unaffected.
export function createLlmClient({ baseUrl, apiKey, fetchImpl = fetch, timeoutMs = 120000 }) {
  const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' };

  return {
    async chat({ messages, system, model, maxTokens = 1024, tools }) {
      const body = { messages, max_tokens: maxTokens };
      if (system) body.system = system;
      if (model) body.model = model;
      // Omitted, null, and [] all mean "no tools" to the service; forward only a
      // non-empty definition list so the text-only path stays byte-for-byte as
      // it was.
      if (Array.isArray(tools) && tools.length > 0) body.tools = tools;

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
      // stop_reason and content_blocks drive the tool loop: they detect a
      // tool_use turn and let it replay the assistant turn verbatim. They stay
      // undefined on text-only responses.
      return {
        content: data.content,
        model: data.model,
        usage: data.usage,
        stop_reason: data.stop_reason,
        content_blocks: data.content_blocks,
      };
    },
  };
}
