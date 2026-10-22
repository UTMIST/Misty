// The document tools the helper exposes to the model (#71). These are the only
// retrieval surface the model can drive, and they are built llmSafe: callers
// register them through a provider whose `llmSafe === true`.
//
// The security-critical invariant: the trusted retrieval context (person for a
// DM, channel team-union for a shared channel) is supplied by the EXECUTOR via
// `ctx.context` and is bound outside the model loop. The model's `input` carries
// only the query or doc id; any scope-shaped field a model might inject into
// `input` is ignored. A tool never reads scope from `input`.
//
// Context shape (exactly one, matching docClient.searchDocs):
//   { onBehalfOf: personId }   -> DM / person
//   { guildId, channelId }     -> shared channel
//
// Residual risk, prompt injection via retrieved content: snippets and document
// bodies flow back to the model as tool results, so a document the asker is
// authorized to see can carry instructions the model may follow. Scope binding
// (above) contains the blast radius. Injected text cannot widen the context or
// reach a document outside it, so the worst case is a steered answer within the
// asker's own scope, never disclosure of anything they could not already see.
// There is no content-level sanitization here; treat model output as untrusted.
export const llmSafe = true;

const MAX_QUERY_LEN = 2000; // service caps query at 2000 code points
const MAX_RESULTS = 10; // service default k
// Byte budget for a fetched document body before it rides back in a tool_result
// (and into the revalidation fingerprint). The real governing limit is the
// service's per-block MAX_JSON_BYTES (65_536): a ToolResultBlock whose content
// strings sum past that is rejected, failing the *next* /chat request wholesale.
// So bound the body in UTF-8 *bytes* (not UTF-16 units), well under 64 KiB, to
// leave room for the title, keys, and JSON overhead in the same block.
const MAX_DOC_CONTENT_BYTES = 49152; // 48 KiB
// Search returns source chunks, not UI previews. Preserve their detail within
// the same per-text byte cap; packHits still enforces the whole-result budget.
const MAX_SEARCH_CHUNK_BYTES = MAX_DOC_CONTENT_BYTES;
const MAX_TITLE_LEN = 300; // units; a label, bounded so a fat title can't blow the block
const MAX_URL_LEN = 2048; // units; real links fit, pathological ones can't bloat a hit
// Real ids are snowflakes ([0-9]{1,20}); this matches get_document's input cap.
// Bounding the one remaining service-controlled field keeps a pathological id
// from bloating a hit past the aggregate budget (which would otherwise drop the
// whole result set, since packHits stops at the first hit that won't fit).
const MAX_DOC_ID_LEN = 128;
// Aggregate byte budget for the whole `{ results: [...] }` block a search
// returns. Per-field caps bound a single hit, but ten hits (each a fat title +
// a ~2 KB url + a snippet, all multibyte) still sum past the service's 64 KiB
// per-block MAX_JSON_BYTES, so the combined result 422s the next /chat request.
// Budget the serialized block (a conservative upper bound on the string bytes
// the service actually counts) and keep it under the limit with headroom.
const MAX_SEARCH_RESULT_BYTES = 61440; // 60 KiB

const utf8Len = (value) => Buffer.byteLength(value, 'utf8');
const jsonLen = (value) => utf8Len(JSON.stringify(value));

// Truncate to at most `max` UTF-16 code units without splitting a surrogate
// pair. A naive slice can cut an astral character (e.g. an emoji) in half,
// leaving a lone surrogate; that is not valid UTF-8, and the /chat service
// rejects the whole request (422) before the model runs. If the cut would land
// just after a high surrogate, drop it so the result stays well-formed.
function truncateUnits(value, max) {
  if (value.length <= max) return value;
  const last = value.charCodeAt(max - 1);
  const end = last >= 0xd800 && last <= 0xdbff ? max - 1 : max;
  return value.slice(0, end);
}

// Truncate to at most `maxBytes` of UTF-8 without splitting a character. Iterate
// by code point (never between surrogate halves) and stop before the budget is
// exceeded, so the result is always well-formed and within the byte limit.
function truncateUtf8(value, maxBytes) {
  if (utf8Len(value) <= maxBytes) return value;
  let bytes = 0;
  let end = 0;
  for (const ch of value) {
    const next = bytes + utf8Len(ch);
    if (next > maxBytes) break;
    bytes = next;
    end += ch.length; // 1 for BMP, 2 for an astral pair
  }
  return value.slice(0, end);
}

// Bound an optional service-provided string field (title/url) in units, leaving
// null/absent as-is. These ride in the model-visible content, so a pathological
// value must not let one hit overflow the block.
function boundField(value, max) {
  return typeof value === 'string' ? truncateUnits(value, max) : (value ?? null);
}

function normalizeString(value, max) {
  if (typeof value !== 'string') return '';
  return truncateUnits(value.trim(), max);
}

// Project a /search hit (SearchHit) down to what the model needs to answer and
// cite. We forward the ids, title, source url, and a bounded text snippet — not
// raw service internals. `citation` carries the chunk descriptor for access
// revalidation and source rendering.
function projectHit(hit) {
  const docId = boundField(hit?.doc_id, MAX_DOC_ID_LEN);
  const text = typeof hit?.text === 'string' ? hit.text : '';
  const title = boundField(hit?.title, MAX_TITLE_LEN);
  const url = boundField(hit?.url, MAX_URL_LEN);
  return {
    doc_id: docId,
    title,
    url,
    snippet: text ? truncateUtf8(text, MAX_SEARCH_CHUNK_BYTES) : undefined,
    ...(utf8Len(text) > MAX_SEARCH_CHUNK_BYTES ? { truncated: true } : {}),
    citation: {
      doc_id: docId,
      title,
      url,
      chunk: hit?.chunk,
    },
  };
}

// The model-visible shape of a hit: the citation descriptor rides alongside the
// content (surfaced by the executor), it is not sent back to the model.
function forModelShape({ citation, ...rest }) {
  return rest;
}

// Admit projected hits in rank order while the serialized results block stays
// within `maxBytes`. Per-field caps bound one hit, but ten can still sum past
// the service's per-block limit, so cap the aggregate here, before fingerprints
// are recorded. We drop whole trailing hits rather than trimming a kept hit's
// fields: a kept hit must stay byte-for-byte equal to projectHit(), or
// revalidation (which re-projects each current hit and matches fingerprints)
// would reject it. A dropped hit is never shown, cited, or fingerprinted, so
// withholding it is safe. Later hits only add bytes, so the first overflow ends
// admission.
function packHits(projected, maxBytes) {
  const kept = [];
  const forModel = [];
  for (const hit of projected) {
    const candidate = [...forModel, forModelShape(hit)];
    if (jsonLen({ results: candidate }) > maxBytes) break;
    kept.push(hit);
    forModel.push(candidate[candidate.length - 1]);
  }
  return { kept, forModel };
}

// Person context forwards an actor; channel context forwards guild+channel. The
// executor binds exactly one; docClient fails closed on both/neither.
function retrievalArgs(context) {
  if (context?.onBehalfOf != null) return { onBehalfOf: context.onBehalfOf };
  return { guildId: context?.guildId, channelId: context?.channelId };
}

function projectDoc(doc, docId) {
  const full = typeof doc.content === 'string' ? doc.content : (doc.content_snapshot ?? null);
  const truncated = typeof full === 'string' && utf8Len(full) > MAX_DOC_CONTENT_BYTES;
  return {
    found: true,
    doc_id: boundField(doc.id ?? docId, MAX_DOC_ID_LEN),
    title: boundField(doc.title, MAX_TITLE_LEN),
    content: truncated ? truncateUtf8(full, MAX_DOC_CONTENT_BYTES) : full,
    // Tell the model the body was cut so it does not treat a partial read as the
    // whole document. Part of the fingerprint too, so revalidation stays exact.
    ...(truncated ? { truncated: true } : {}),
  };
}

// The executor supplies a per-answer request/deadline gate. Direct tool tests
// may omit it; model-driven calls must always use the executor.
const directRead = (_key, read) => read();

export function createDocumentTools({ docClient }) {
  return [
    {
      definition: {
        name: 'search_documents',
        description:
          'Search the documents the asker is authorized to see and return ranked source passages with their ids and citations. Use these passages to answer; refine the query if the needed detail is absent. A passage is part of a document, not necessarily the whole document. Returns an empty list when nothing authorized matches.',
        input_schema: {
          type: 'object',
          properties: {
            query: { type: 'string', description: 'What to search for, in natural language.' },
          },
          required: ['query'],
        },
      },
      async run(input, ctx) {
        const query = normalizeString(input?.query, MAX_QUERY_LEN);
        if (!query) return { content: { error: 'query is required' }, is_error: true };
        const args = {
          query,
          k: MAX_RESULTS,
          ...retrievalArgs(ctx.context), // trusted, bound by the executor — never from input
        };
        const readDocument = ctx.readDocument ?? directRead;
        const key = JSON.stringify(['search', args]);
        const read = (signal) => docClient.searchDocs(args, { signal });
        const results = await readDocument(key, read);
        const projected = (results ?? []).map(projectHit);
        // Enforce the aggregate block budget: keep as many ranked hits as fit
        // under the 64 KiB per-block limit, dropping the overflow tail so the
        // combined result can't 422 the next /chat request.
        const { kept, forModel } = packHits(projected, MAX_SEARCH_RESULT_BYTES);
        // `citations` ride alongside the model-visible content so the executor
        // can surface sources without the model having to echo them back.
        const citations = kept.filter((r) => r.doc_id).map((r) => r.citation);
        // Re-run the same authorized search before replay/publication. Require
        // every kept chunk and its metadata to remain visible unchanged. Ranking
        // changes can conservatively withhold an answer; missing hits must never
        // be treated as proof of continued access. Only kept hits are
        // fingerprinted: a hit we dropped for size was never used to answer.
        const fingerprints = kept.map((hit) => JSON.stringify(hit));
        return {
          content: { results: forModel },
          citations,
          async revalidate(readForPass = readDocument) {
            if (!fingerprints.length) return true;
            const current = await readForPass(key, read);
            const visible = new Set(current.map((hit) => JSON.stringify(projectHit(hit))));
            return fingerprints.every((hit) => visible.has(hit));
          },
        };
      },
    },
    {
      definition: {
        name: 'get_document',
        description:
          'Fetch one document by its id to read more than a search snippet, for example to follow up on a search hit. Only available when answering a direct message; in a shared channel, cite the source link from search_documents instead. Returns not-found when the asker is not authorized to see the document.',
        input_schema: {
          type: 'object',
          properties: {
            doc_id: { type: 'string', description: 'The id of the document to open.' },
          },
          required: ['doc_id'],
        },
      },
      async run(input, ctx) {
        const docId = normalizeString(input?.doc_id, 128);
        if (!docId) return { content: { error: 'doc_id is required' }, is_error: true };
        // Direct-document fetch is person-scoped only. A shared channel has no
        // authorized direct-document endpoint (the channel grant covers search
        // citations, not arbitrary doc reads), so refuse rather than attempt a
        // read that would either fail or escape the channel's scope.
        const onBehalfOf = ctx.context?.onBehalfOf;
        if (onBehalfOf == null) {
          return {
            content: {
              available: false,
              reason: 'Opening a document by id is only available in a direct message.',
            },
          };
        }
        const readDocument = ctx.readDocument ?? directRead;
        const key = JSON.stringify(['get', docId, onBehalfOf]);
        const read = (signal) =>
          docClient.getDoc(docId, { onBehalfOf, ...(signal ? { signal } : {}) });
        const doc = await readDocument(key, read);
        // A doc the context can't see comes back null (indistinguishable from
        // missing): report not-found, never that it exists but is forbidden.
        if (!doc) return { content: { found: false } };
        const id = doc.id ?? docId;
        const content = projectDoc(doc, docId);
        const fingerprint = JSON.stringify({ content, url: doc.url ?? null });
        return {
          content,
          citations: [{ doc_id: id, title: doc.title ?? null, url: doc.url ?? null }],
          async revalidate(readForPass = readDocument) {
            const current = await readForPass(key, read);
            return Boolean(
              current &&
              JSON.stringify({
                content: projectDoc(current, docId),
                url: current.url ?? null,
              }) === fingerprint,
            );
          },
        };
      },
    },
  ];
}
