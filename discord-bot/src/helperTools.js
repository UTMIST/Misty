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
const SNIPPET_LEN = 400;
// Cap a fetched document body before it rides back in a tool_result (and into
// the revalidation fingerprint). The service rejects any tool payload over 1 MiB
// (MAX_TOOL_PAYLOAD_BYTES), which would fail the *next* /chat request wholesale,
// not just the one tool call; bound well under that and flag truncation so the
// model knows it is not seeing the whole document. Generous vs. SNIPPET_LEN,
// since get_document exists to read past a snippet.
const MAX_DOC_CONTENT_LEN = 20000;

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

function normalizeString(value, max) {
  if (typeof value !== 'string') return '';
  return truncateUnits(value.trim(), max);
}

// Project a /search hit (SearchHit) down to what the model needs to answer and
// cite. We forward the ids, title, source url, and a bounded text snippet — not
// raw service internals. `citation` carries the chunk descriptor for access
// revalidation and source rendering.
function projectHit(hit) {
  const docId = hit?.doc_id ?? null;
  const text = typeof hit?.text === 'string' ? hit.text : '';
  return {
    doc_id: docId,
    title: hit?.title ?? null,
    url: hit?.url ?? null,
    snippet: text ? truncateUnits(text, SNIPPET_LEN) : undefined,
    citation: {
      doc_id: docId,
      title: hit?.title ?? null,
      url: hit?.url ?? null,
      chunk: hit?.chunk,
    },
  };
}

// Person context forwards an actor; channel context forwards guild+channel. The
// executor binds exactly one; docClient fails closed on both/neither.
function retrievalArgs(context) {
  if (context?.onBehalfOf != null) return { onBehalfOf: context.onBehalfOf };
  return { guildId: context?.guildId, channelId: context?.channelId };
}

function projectDoc(doc, docId) {
  const body = typeof doc.content === 'string' ? doc.content : (doc.content_snapshot ?? null);
  const truncated = typeof body === 'string' && body.length > MAX_DOC_CONTENT_LEN;
  return {
    found: true,
    doc_id: doc.id ?? docId,
    title: doc.title ?? null,
    content: truncated ? truncateUnits(body, MAX_DOC_CONTENT_LEN) : body,
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
          'Search the documents the asker is authorized to see and return ranked matches with their ids and source citations. Use this to find information before answering. Returns an empty list when nothing authorized matches.',
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
        // `citations` ride alongside the model-visible content so the executor
        // can surface sources without the model having to echo them back.
        const citations = projected.filter((r) => r.doc_id).map((r) => r.citation);
        const forModel = projected.map(({ citation, ...rest }) => rest);
        // Re-run the same authorized search before replay/publication. Require
        // every original chunk and its metadata to remain visible unchanged.
        // Ranking changes can conservatively withhold an answer; missing hits
        // must never be treated as proof of continued access.
        const fingerprints = projected.map((hit) => JSON.stringify(hit));
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
