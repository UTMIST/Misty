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

// Authorized-retrieval scoping for POST /search (shared #176 contract). The
// service requires EXACTLY one authenticated context and derives the visible
// team set itself; the caller never passes teams, vectors, or identity in the
// body:
//   { onBehalfOf: personId }          -> DM / person: X-On-Behalf-Of, needs the
//                                        key's act-as-user scope.
//   { guildId, channelId }            -> shared channel: X-Guild-ID + X-Channel-ID
//                                        (decimal snowflakes), needs act-as-channel.
//                                        The service resolves the channel's
//                                        configured teams; an unconfigured channel
//                                        returns { results: [] }.
// The bot decides scope from the answer's destination, never from model
// arguments. A missing, empty, or ambiguous (both/neither) context is a
// programming error and fails closed by throwing before any request leaves the
// bot — there is no unfiltered search to fall back to.
function searchContextHeaders({ onBehalfOf, guildId, channelId }) {
  const hasPerson = onBehalfOf != null;
  const hasChannel = guildId != null || channelId != null;
  if (hasPerson === hasChannel) {
    throw new DocUnavailable('retrieval context must be exactly one of person or channel');
  }
  if (hasPerson) {
    if (typeof onBehalfOf !== 'string' || !onBehalfOf) {
      throw new DocUnavailable('person context requires a non-empty actor id');
    }
    return { 'X-On-Behalf-Of': onBehalfOf };
  }
  if (!guildId || !channelId) {
    throw new DocUnavailable('channel context requires both a guild id and a channel id');
  }
  return { 'X-Guild-ID': String(guildId), 'X-Channel-ID': String(channelId) };
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

    // Authorized hybrid chunk search (shared #176 contract). Scoped by `context`
    // (see searchContextHeaders): person for DMs, channel team-union for shared
    // channels. `k` is forwarded only when given, so the service default (10)
    // applies otherwise. Returns the ranked `results` array verbatim (one chunk
    // per hit; a doc may appear more than once); an empty set is a normal answer,
    // not an error.
    //
    // NOTE (#71/#210 coordination): this extends the member-search client shape
    // from #289 (`searchDocs({ query, k, onBehalfOf })`) with the channel path
    // (`guildId`/`channelId`). Person-only callers behave exactly as in #289.
    async searchDocs({ query, k, onBehalfOf, guildId, channelId }) {
      const body = { query };
      if (k !== undefined) body.k = k;
      const resp = await send('/search', {
        method: 'POST',
        headers: searchContextHeaders({ onBehalfOf, guildId, channelId }),
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw unavailable(resp.status, 'POST /search');
      const parsed = await parseJson(resp);
      if (!Array.isArray(parsed?.results)) {
        throw new DocUnavailable('malformed doc-service response');
      }
      return parsed.results;
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
