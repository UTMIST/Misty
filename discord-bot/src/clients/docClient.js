import { createHttpClient } from './httpClient.js';

export class DocUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = 'DocUnavailable';
  }
}

export class DocBadReference extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'DocBadReference';
    this.detail = detail;
  }
}

// Assert an actor for a read so the doc service filters visibility as that user
// (on-behalf-of, gated by the key's act-as-user scope). Omit the header entirely
// when there's no actor — the service then applies its no-actor policy.
function oboHeader(onBehalfOf) {
  return onBehalfOf ? { headers: { 'X-On-Behalf-Of': onBehalfOf } } : {};
}

export function createDocClient({ baseUrl, apiKey, fetchImpl = fetch }) {
  const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' };
  const { send, parseJson } = createHttpClient({
    baseUrl,
    headers,
    fetchImpl,
    networkError: () => new DocUnavailable('network error reaching doc service'),
    parseError: () => new DocUnavailable('malformed doc-service response'),
  });

  function unavailable(status, context) {
    // Surface unexpected statuses (notably 401/403 from a bad or mis-scoped
    // API key) in logs — otherwise they render to users as a generic
    // "temporarily unavailable" with no clue it's a config problem.
    console.warn(`docClient: ${context} -> HTTP ${status}`);
    return new DocUnavailable(`doc service returned ${status}`);
  }

  return {
    async ingestDoc({ url, sourceId, title, description, owningTeamId, owningPersonId, tags }) {
      const body = { url };
      if (sourceId !== undefined) body.source_id = sourceId;
      if (title !== undefined) body.title = title;
      if (description !== undefined) body.description = description;
      if (owningTeamId !== undefined) body.owning_team_id = owningTeamId;
      if (owningPersonId !== undefined) body.owning_person_id = owningPersonId;
      if (tags !== undefined) body.tags = tags;
      const resp = await send('/docs', { method: 'POST', body: JSON.stringify(body) });
      if (resp.ok) return parseJson(resp);
      if (resp.status === 400) {
        const b = await resp.json().catch(() => ({}));
        throw new DocBadReference(b.detail ?? 'bad reference');
      }
      throw unavailable(resp.status, 'POST /docs');
    },

    async listDocs({ owningTeamId, owningPersonId, sourceId, tag, activeOnly, onBehalfOf } = {}) {
      const qs = new URLSearchParams();
      if (owningTeamId) qs.set('owning_team_id', owningTeamId);
      if (owningPersonId) qs.set('owning_person_id', owningPersonId);
      if (sourceId) qs.set('source_id', sourceId);
      if (tag) qs.set('tag', tag);
      if (activeOnly !== undefined) qs.set('active_only', String(activeOnly));
      const qstr = qs.toString();
      const resp = await send(qstr ? `/docs?${qstr}` : '/docs', oboHeader(onBehalfOf));
      if (!resp.ok) throw unavailable(resp.status, 'GET /docs');
      return parseJson(resp);
    },

    async getDoc(id, { onBehalfOf } = {}) {
      const resp = await send(`/docs/${encodeURIComponent(id)}`, oboHeader(onBehalfOf));
      if (resp.status === 404) return null;
      if (!resp.ok) throw unavailable(resp.status, 'GET /docs/{id}');
      return parseJson(resp);
    },

    async deactivateDoc(id) {
      const resp = await send(`/docs/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ active: false }),
      });
      if (resp.status === 404) return null;
      if (!resp.ok) throw unavailable(resp.status, 'PATCH /docs/{id}');
      return parseJson(resp);
    },
  };
}
