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
// Residual risk, prompt injection via retrieved content: passages and document
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
// service's per-block MAX_JSON_BYTES (65_536), including JSON escaping and
// metadata. Bound both raw text and serialized content before the next /chat.
const MAX_DOC_CONTENT_BYTES = 49152; // 48 KiB
// Search returns source chunks, not UI previews. Preserve their detail within
// the same per-text byte cap; packHits still enforces the whole-result budget.
const MAX_SEARCH_CHUNK_BYTES = MAX_DOC_CONTENT_BYTES;
const MAX_TITLE_LEN = 300; // units; a label, bounded so a fat title can't blow the block
const MAX_URL_LEN = 2048; // units; real links fit, pathological ones can't bloat a hit
// Document ids use the same cap as get_document's input.
// Bounding service-controlled metadata leaves room for evidence in every hit.
const MAX_DOC_ID_LEN = 128;
// Aggregate byte budget for the whole `{ results: [...] }` block a search
// returns. Also use this headroom for direct-document results. Raw UTF-8 caps
// alone do not account for JSON escaping, metadata, or multiple passages.
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

// Bound text by raw UTF-8 bytes and its JSON-escaped bytes (excluding quotes).
// Both projections use this so replay fingerprints describe the actual evidence.
// Iterate by code point only when a cap bites, never between surrogate halves.
function boundText(value, maxBytes, maxEscapedBytes) {
  if (
    typeof value !== 'string' ||
    (utf8Len(value) <= maxBytes && jsonLen(value) - 2 <= maxEscapedBytes)
  ) {
    return { text: value, truncated: false };
  }
  let bytes = 0;
  let escapedBytes = 0;
  let end = 0;
  for (const ch of value) {
    const size = utf8Len(ch);
    const escapedSize = ch < ' ' || ch === '"' || ch === '\\' ? jsonLen(ch) - 2 : size;
    if (bytes + size > maxBytes || escapedBytes + escapedSize > maxEscapedBytes) break;
    bytes += size;
    escapedBytes += escapedSize;
    end += ch.length; // 1 for BMP, 2 for an astral pair
  }
  return { text: value.slice(0, end), truncated: true };
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
// cite. We forward the ids, title, source url, and a bounded source passage — not
// raw service internals. `citation` carries the chunk descriptor for access
// revalidation and source rendering. Keep the `snippet` field name for consumers.
function projectHit(hit) {
  const docId = boundField(hit?.doc_id, MAX_DOC_ID_LEN);
  const title = boundField(hit?.title, MAX_TITLE_LEN);
  const url = boundField(hit?.url, MAX_URL_LEN);
  const fields = { doc_id: docId, title, url };
  // Each projection must fit by itself, independently of rank or other hits.
  // Reserve the truncation marker and complete envelope before bounding text.
  const overhead = jsonLen({ results: [{ ...fields, snippet: '', truncated: true }] });
  const { text, truncated } = boundText(
    typeof hit?.text === 'string' ? hit.text : '',
    MAX_SEARCH_CHUNK_BYTES,
    MAX_SEARCH_RESULT_BYTES - overhead,
  );
  return {
    ...fields,
    snippet: text || undefined,
    ...(truncated ? { truncated: true } : {}),
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
// are recorded. We skip whole overflowing hits rather than trimming a kept hit's
// fields: a kept hit must stay byte-for-byte equal to projectHit(), or
// revalidation (which re-projects each current hit and matches fingerprints)
// would reject it. A dropped hit is never shown, cited, or fingerprinted, so
// withholding it is safe. A later, smaller hit can still fit; preserve its rank
// relative to the other kept hits. Count each row once, plus envelope and commas.
function packHits(projected, maxBytes) {
  const kept = [];
  const forModel = [];
  let bytes = jsonLen({ results: [] });
  for (const hit of projected) {
    const candidate = forModelShape(hit);
    const added = jsonLen(candidate) + (kept.length ? 1 : 0);
    if (bytes + added > maxBytes) continue;
    kept.push(hit);
    forModel.push(candidate);
    bytes += added;
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
  const fields = {
    found: true,
    doc_id: boundField(doc.id ?? docId, MAX_DOC_ID_LEN),
    title: boundField(doc.title, MAX_TITLE_LEN),
  };
  const overhead = jsonLen({ ...fields, content: '', truncated: true });
  const { text, truncated } = boundText(
    full,
    MAX_DOC_CONTENT_BYTES,
    MAX_SEARCH_RESULT_BYTES - overhead,
  );
  return {
    ...fields,
    content: text,
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
        // under the 64 KiB per-block limit, skipping overflowing hits so the
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
            const visible = new Set((current ?? []).map((hit) => JSON.stringify(projectHit(hit))));
            return fingerprints.every((hit) => visible.has(hit));
          },
        };
      },
    },
    {
      definition: {
        name: 'get_document',
        description:
          'Fetch one document by its id to read beyond a search passage, for example to follow up on a search hit. Only available when answering a direct message; in a shared channel, use the authorized passages and source links from search_documents instead. Returns not-found when the asker is not authorized to see the document.',
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
        // passages and citations, not arbitrary doc reads), so refuse a read
        // that would either fail or escape the channel's scope.
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
