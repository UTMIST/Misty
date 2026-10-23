import test from 'node:test';
import assert from 'node:assert/strict';
import { answerHelperTurns, prepareHelperReply, WITHHELD_ANSWER } from '../src/helperFlow.js';
import { createHelperService } from '../src/services/helperService.js';
import { createToolExecutor, OUTCOME } from '../src/toolExecutor.js';
import { createDocClient } from '../src/clients/docClient.js';
import { buildToolRegistry } from '../src/toolRegistry.js';
import { createDocumentTools } from '../src/helperTools.js';
import {
  handleMention,
  handleDirectMessage,
  sendHelperReply,
} from '../src/adapters/discord/mentions.js';
import { renderHelperAnswer } from '../src/messages/helper.js';

const principal = { person: { id: 'person-1', display_name: 'Pat' } };
const user = (text) => ({ role: 'user', authorId: 'member-1', authorName: 'Pat', text });
const channel = { kind: 'channel', guildId: '100', channelId: '200' };
const hit = {
  doc_id: 'doc-1',
  title: 'Test document',
  url: 'https://example.test/document',
  text: 'RESTRICTED_SENTINEL',
  chunk: { ordinal: 0, start_offset: 0, end_offset: 19, source_content_hash: 'abc' },
};
const use = (name = 'search_documents', input = { query: 'test document' }) => ({
  content: '',
  stop_reason: 'tool_use',
  content_blocks: [{ type: 'tool_use', id: 't1', name, input }],
  usage: { output_tokens: 8 },
});
const answer = (content = hit.text) => ({
  content,
  stop_reason: 'end_turn',
  usage: { output_tokens: 8 },
});
function app(llmClient, docClient, limits = {}) {
  const directory = {
    getPersonByDiscordId: async () => principal.person,
    listTeams: async () => [],
    listMemberships: async () => [],
  };
  const registry = buildToolRegistry([
    { name: 'docs', llmSafe: true, tools: createDocumentTools({ docClient }) },
  ]);
  return {
    directory,
    helperToolsEnabled: true,
    helperRequestLimiter: { tryConsume: () => ({ allowed: true }) },
    helperService: createHelperService({ llmClient, directory }),
    toolExecutor: createToolExecutor({ llmClient, registry, ...limits }),
  };
}

for (const enabled of [true, false]) {
  test(`old document-derived answers never reach the model (tools enabled=${enabled})`, async () => {
    const sent = [];
    const ctx = app(
      {
        chat: async (args) => {
          sent.push(structuredClone(args));
          return answer('Follow-up');
        },
      },
      {
        searchDocs: async () => [],
        getDoc: async () => null,
      },
    );
    ctx.helperToolsEnabled = enabled;
    await answerHelperTurns(
      ctx,
      [user('Read the document'), { role: 'assistant', text: hit.text }, user('Tell me more')],
      principal,
      channel,
    );
    assert.equal(JSON.stringify(sent).includes(hit.text), false);
    assert.match(JSON.stringify(sent[0].messages), /Read the document/);
    assert.match(JSON.stringify(sent[0].messages), /Tell me more/);
  });
}

for (const destination of [channel, { kind: 'person' }]) {
  for (const failure of ['revoked', 'outage', 'changed']) {
    test(`${destination.kind} access ${failure} during generation withholds the answer`, async () => {
      let changed = false,
        calls = 0;
      const ctx = app(
        {
          chat: async () => {
            if (calls++ === 0) return use();
            changed = true;
            return answer();
          },
        },
        {
          searchDocs: async (args) => {
            assert.deepEqual(args, {
              query: 'test document',
              k: 10,
              ...(destination.kind === 'person'
                ? { onBehalfOf: 'person-1' }
                : { guildId: '100', channelId: '200' }),
            });
            if (!changed) return [hit];
            if (failure === 'outage') throw new Error('lookup unavailable');
            return failure === 'changed' ? [{ ...hit, text: 'Updated content' }] : [];
          },
        },
      );
      const result = await answerHelperTurns(ctx, [user('Read')], principal, destination);
      assert.equal(calls, 2);
      assert.equal(result, WITHHELD_ANSWER);
    });
  }
}

test('revocation after a tool read prevents sending its content to another model call', async () => {
  let reads = 0,
    calls = 0;
  const ctx = app(
    {
      chat: async () => {
        calls++;
        return use();
      },
    },
    {
      searchDocs: async () => (reads++ === 0 ? [hit] : []),
    },
  );
  assert.equal(await answerHelperTurns(ctx, [user('Read')], principal, channel), WITHHELD_ANSWER);
  assert.equal(calls, 1);
});

test('DM direct-document reads are reauthorized before publication', async () => {
  let allowed = true,
    calls = 0;
  const ctx = app(
    {
      chat: async () => {
        if (calls++ === 0) return use('get_document', { doc_id: hit.doc_id });
        allowed = false;
        return answer();
      },
    },
    {
      getDoc: async (id, opts) => {
        assert.equal(id, hit.doc_id);
        assert.equal(opts.onBehalfOf, 'person-1');
        assert.ok(opts.signal instanceof AbortSignal);
        return allowed ? { id, title: hit.title, content_snapshot: hit.text, url: hit.url } : null;
      },
    },
  );
  assert.equal(
    await answerHelperTurns(ctx, [user('Open doc-1')], principal, { kind: 'person' }),
    WITHHELD_ANSWER,
  );
});

test('private direct-ID follow-up cannot read a document into a channel', async () => {
  let calls = 0;
  const ctx = app(
    {
      chat: async (args) => {
        if (calls++ === 0) return use('get_document', { doc_id: hit.doc_id, onBehalfOf: 'admin' });
        assert.equal(args.messages.at(-1).content[0].content.available, false);
        return answer('Open it in a DM.');
      },
    },
    { getDoc: async () => assert.fail('no person read may run for a channel') },
  );
  assert.equal(
    await answerHelperTurns(ctx, [user('Open doc-1')], principal, channel),
    'Open it in a DM.',
  );
});

test('authorized citations survive the real Discord mention and DM delivery paths', async () => {
  for (const kind of ['channel', 'person']) {
    let calls = 0;
    const sent = [];
    const ctx = app(
      { chat: async () => (calls++ === 0 ? use() : answer('Here is the answer.')) },
      { searchDocs: async () => [hit] },
    );
    const target = {
      id: '200',
      isThread: () => false,
      sendTyping: async () => {},
      send: async (part) => sent.push(part),
      messages: { fetch: async () => new Map() },
    };
    const message = {
      id: '300',
      author: { id: 'member-1' },
      content: 'Read the document',
      channel: target,
      guildId: '100',
      startThread: async () => target,
    };
    await (kind === 'channel' ? handleMention : handleDirectMessage)(message, {
      appContext: ctx,
      botId: 'bot-1',
    });
    assert.match(sent.join('\n'), /Here is the answer/);
    assert.ok(sent.join('\n').includes(hit.url));
  }
});

test('oversized citations preserve complete links through channel and DM delivery', async () => {
  const urls = [1850, 1970, 1971, 1972, 1978, 1980].map((size) => {
    const url = 'https://example.test/' + 'a'.repeat(size);
    return [url, url];
  });
  urls.push(
    ...[1953, 1959, 1970].map((size) => [
      'https://example.test/' + 'a'.repeat(size) + '?q=```',
      'https://example.test/' + 'a'.repeat(size) + '?q=%60%60%60',
    ]),
    ['https://example.test/' + ')'.repeat(640), 'https://example.test/' + '%29'.repeat(640)],
    ['https://example.test/' + ')'.repeat(700), 'https://example.test/' + '%29'.repeat(700)],
  );
  for (const [url, href] of urls) {
    const longHit = {
      ...hit,
      title: '[WIP] @everyone '.repeat(10),
      url,
    };
    for (const kind of ['channel', 'person']) {
      let calls = 0;
      const sent = [];
      const ctx = app(
        { chat: async () => (calls++ === 0 ? use() : answer('Here is the answer.')) },
        { searchDocs: async () => [longHit] },
      );
      const target = {
        id: '200',
        isThread: () => false,
        sendTyping: async () => {},
        send: async (part) => sent.push(part),
        messages: { fetch: async () => new Map() },
      };
      const message = {
        id: '300',
        author: { id: 'member-1' },
        content: 'Read the document',
        channel: target,
        guildId: '100',
        startThread: async () => target,
      };
      await (kind === 'channel' ? handleMention : handleDirectMessage)(message, {
        appContext: ctx,
        botId: 'bot-1',
      });
      assert.match(sent.join('\n'), /Here is the answer/);
      assert.ok(sent.every((part) => part.length <= 2000));
      if (href.length + 2 <= 2000) {
        assert.equal(sent.filter((part) => part.includes(`<${href}>`)).length, 1);
        const source = renderHelperAnswer('', [longHit]).split('\n').at(-1);
        assert.ok(sent.some((part) => part.includes(source)));
      } else {
        assert.ok(sent.some((part) => part.includes('Source link is too long to display.')));
        assert.equal(sent.join('\n').includes('https://example.test/'), false);
      }
    }
  }
});

test('a generated reply is checked again at the send boundary and between parts', async () => {
  let allowed = true,
    calls = 0;
  const ctx = app(
    { chat: async () => (calls++ === 0 ? use() : answer('x'.repeat(4100))) },
    { searchDocs: async () => (allowed ? [hit] : []) },
  );
  const reply = await prepareHelperReply(ctx, [user('Read')], principal, channel);
  const sent = [];
  await sendHelperReply(async (part) => {
    sent.push(part);
    allowed = false;
  }, reply);
  assert.equal(sent.length, 2);
  assert.ok(sent[0].length <= 2000);
  assert.equal(sent[1], WITHHELD_ANSWER);
  const later = [];
  await sendHelperReply(async (part) => later.push(part), reply);
  assert.deepEqual(later, [WITHHELD_ANSWER]);
});

test('channel evidence beyond 6 KiB reaches the model and is rechecked before delivery', async () => {
  const prefix = 'Background context. '.repeat(400);
  const detail = 'The restore target is four hours.';
  let text = prefix + detail;
  let calls = 0;
  const ctx = app(
    {
      chat: async ({ messages }) => {
        if (calls++ === 0) return use();
        const result = messages.at(-1).content[0].content.results[0];
        assert.equal(result.snippet, text);
        return answer(detail);
      },
    },
    {
      searchDocs: async (args) => {
        assert.deepEqual(args, {
          query: 'test document',
          k: 10,
          guildId: '100',
          channelId: '200',
        });
        return [{ ...hit, text }];
      },
      getDoc: async () => assert.fail('channel passages need no direct-document read'),
    },
  );
  const reply = await prepareHelperReply(ctx, [user('Restore target?')], principal, channel);
  const sent = [];
  await sendHelperReply(async (part) => sent.push(part), reply);
  assert.ok(sent.join('\n').includes(detail));
  assert.ok(sent.join('\n').includes(hit.url));
  // A later send must check again, including the detail past the old cutoff.
  text = prefix + 'The restore target is eight hours.';
  const later = [];
  await sendHelperReply(async (part) => later.push(part), reply);
  assert.deepEqual(later, [WITHHELD_ANSWER]);
});

test('source rendering deduplicates links and rejects unsafe URL schemes', () => {
  const rendered = renderHelperAnswer('Answer', [
    hit,
    hit,
    { doc_id: 'evil', title: 'bad', url: 'javascript:alert(1)' },
    { doc_id: 'credentials', title: 'private', url: 'https://user:password@example.test/doc' },
    { doc_id: 'invalid', title: 'invalid', url: 'not a URL' },
  ]);
  assert.equal(rendered, `Answer\n\nSources:\n• [Test document](<${hit.url}>)`);
  assert.equal(renderHelperAnswer('Answer', [{ url: 'javascript:alert(1)' }]), 'Answer');
});

test('source titles cannot escape masked links or trigger mentions', () => {
  const title = '](https://evil.test)\n@everyone \\`*_{}<>#|~';
  const rendered = renderHelperAnswer('Answer', [{ ...hit, title }]);
  const escaped = '\\]\\(https://evil.test\\) @\u200beveryone \\\\\\`\\*\\_\\{\\}\\<\\>\\#\\|\\~';
  assert.equal(rendered, `Answer\n\nSources:\n• [${escaped}](<${hit.url}>)`);
});

test('bracketed source titles retain the existing Markdown escaping', () => {
  assert.equal(
    renderHelperAnswer('Answer', [{ ...hit, title: '[WIP] Plan' }]),
    `Answer\n\nSources:\n• [\\[WIP\\] Plan](<${hit.url}>)`,
  );
});

test('source hrefs encode masked-link delimiters and whitespace', () => {
  const url = 'https://example.test/a(b) c<d>?q=hello world)#fragment)';
  assert.equal(
    renderHelperAnswer('Answer', [{ ...hit, url }]),
    'Answer\n\nSources:\n• [Test document](<https://example.test/a%28b%29%20c%3Cd%3E?q=hello%20world%29#fragment%29>)',
  );
});

test('untitled sources still have a clickable label', () => {
  for (const title of [undefined, null, '', ' \n\t ']) {
    assert.equal(
      renderHelperAnswer('Answer', [{ ...hit, title }]),
      `Answer\n\nSources:\n• [Document](<${hit.url}>)`,
    );
  }
});

test('production context wiring enforces the guard through the HTTP clients', async (t) => {
  const { createAppContext } = await import('../src/context.js');
  let calls = 0,
    authorized = true,
    searches = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === 'http://llm/chat') {
      const body = JSON.parse(options.body);
      assert.ok(body.tools.length);
      if (calls++ === 0) return Response.json(use());
      authorized = false;
      return Response.json(answer());
    }
    if (url === 'http://docs/search') {
      searches++;
      assert.equal(options.headers['X-On-Behalf-Of'], undefined);
      assert.equal(options.headers['X-Guild-ID'], '100');
      assert.equal(options.headers['X-Channel-ID'], '200');
      return Response.json({ results: authorized ? [hit] : [] });
    }
    if (url.startsWith('http://directory/teams') || url.startsWith('http://directory/memberships'))
      return Response.json([]);
    if (url === 'http://directory/people/by-identifier/discord/requester')
      return Response.json(principal.person);
    assert.fail(`unexpected offline HTTP call: ${url}`);
  });
  const ctx = createAppContext({
    directoryBaseUrl: 'http://directory',
    directoryApiKey: 'test',
    docBaseUrl: 'http://docs',
    docApiKey: 'test',
    llmBaseUrl: 'http://llm',
    llmApiKey: 'test',
    verificationBaseUrl: 'http://verification',
    verificationApiKey: 'test',
    helperToolsEnabled: true,
  });
  const result = await answerHelperTurns(ctx, [user('Read')], principal, channel, {
    discordUserId: 'requester',
  });
  assert.equal(result, WITHHELD_ANSWER);
  assert.equal(calls, 2);
  assert.equal(searches, 3);
});

for (const repeated of [false, true]) {
  test(`four tool rounds have bounded searches and fresh delivery checks (repeated=${repeated})`, async () => {
    let searches = 0;
    let chats = 0;
    let allowed = true;
    const ctx = app(
      {
        chat: async () => {
          chats++;
          return chats <= 4
            ? use('search_documents', { query: repeated ? 'same' : `query ${chats}` })
            : answer();
        },
      },
      {
        searchDocs: async () => {
          searches++;
          return allowed ? [hit] : [];
        },
      },
    );
    const reply = await prepareHelperReply(ctx, [user('Read')], principal, channel);
    assert.match(reply.content, /RESTRICTED_SENTINEL/);
    // Different queries: 4 initial reads + (1+2+3+4) replay checks + 4 final checks.
    // Same query: all original fingerprints use one fresh read per check pass.
    assert.equal(searches, repeated ? 9 : 18);
    assert.equal(chats, 5);
    assert.equal(await reply.canPublish(), true);
    assert.equal(searches, repeated ? 10 : 22);
    allowed = false;
    assert.equal(await reply.canPublish(), false);
    const before = searches;
    allowed = true;
    assert.equal(await reply.canPublish(), false); // failure stays closed
    assert.equal(searches, before);
  });
}

test('sharing a lookup in one check pass still verifies every earlier fingerprint', async () => {
  let searches = 0;
  let chats = 0;
  const ctx = app(
    { chat: async () => (++chats <= 2 ? use() : answer()) },
    {
      searchDocs: async () => {
        searches++;
        // Change the same query's evidence on its second tool invocation.
        return [searches <= 2 ? hit : { ...hit, text: 'Replacement content' }];
      },
    },
  );
  assert.equal(await answerHelperTurns(ctx, [user('Read')], principal, channel), WITHHELD_ANSWER);
  assert.equal(chats, 2); // neither version is replayed after the change
  assert.equal(searches, 4);
});

test('the document request cap includes every Discord message part and cannot be reset', async () => {
  let searches = 0;
  let chats = 0;
  const ctx = app(
    {
      chat: async () => {
        chats++;
        return chats <= 4
          ? use('search_documents', { query: `query ${chats}` })
          : answer('x'.repeat(10000));
      },
    },
    { searchDocs: async () => (++searches, [hit]) },
  );
  const reply = await prepareHelperReply(ctx, [user('Read')], principal, channel);
  const sent = [];
  await sendHelperReply(async (part) => sent.push(part), reply);
  assert.equal(searches, 32);
  assert.equal(sent.length, 4); // three checked parts, then one safe fallback
  assert.equal(sent.at(-1), WITHHELD_ANSWER);
  assert.equal(await reply.canPublish(), false);
  assert.equal(searches, 32);
});

test('document failures spend the same request budget as successful reads', async () => {
  let searches = 0;
  let chats = 0;
  const ctx = app(
    { chat: async () => (++chats, use()) },
    {
      searchDocs: async () => {
        searches++;
        throw new Error('offline');
      },
    },
    { maxDocumentRequests: 1 },
  );
  const result = await ctx.toolExecutor.run({
    messages: [{ role: 'user', content: 'Read' }],
    context: { onBehalfOf: principal.person.id },
  });
  assert.deepEqual(result, { outcome: OUTCOME.EXHAUSTED });
  assert.equal(searches, 1);
  assert.equal(chats, 2);
});

for (const tool of ['search_documents', 'get_document']) {
  test(`the deadline aborts a stalled ${tool} response body through the real HTTP client`, async () => {
    let requestSignal;
    let chats = 0;
    const docClient = createDocClient({
      baseUrl: 'http://docs.invalid',
      apiKey: 'fake-test-key',
      fetchImpl: async (_url, { signal }) => {
        requestSignal = signal;
        return {
          ok: true,
          json: () =>
            new Promise((_, reject) => {
              signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
            }),
        };
      },
    });
    const ctx = app(
      { chat: async () => (++chats, use(tool, { query: 'Read', doc_id: hit.doc_id })) },
      docClient,
      { maxDurationMs: 50 },
    );
    const result = await ctx.toolExecutor.run({
      messages: [{ role: 'user', content: 'Read' }],
      context: { onBehalfOf: principal.person.id },
    });
    assert.deepEqual(result, { outcome: OUTCOME.EXHAUSTED });
    assert.equal(requestSignal.aborted, true);
    assert.equal(chats, 1);
  });
}

test('generation and later delivery share one deadline', async () => {
  let searches = 0;
  let chats = 0;
  const ctx = app(
    { chat: async () => (++chats === 1 ? use() : answer()) },
    { searchDocs: async () => (++searches, [hit]) },
    { maxDurationMs: 50 },
  );
  const reply = await prepareHelperReply(ctx, [user('Read')], principal, channel);
  assert.match(reply.content, /RESTRICTED_SENTINEL/);
  await new Promise((resolve) => setTimeout(resolve, 60));
  const before = searches;
  const sent = [];
  await sendHelperReply(async (part) => sent.push(part), reply);
  assert.deepEqual(sent, [WITHHELD_ANSWER]);
  assert.equal(searches, before);
});

for (const configured of [true, false]) {
  test(`thread retrieval uses its parent's full snowflake strings (configured=${configured})`, async () => {
    const guildId = '987654321098765432';
    const parentId = '123456789012345678';
    const threadId = '223456789012345679';
    const requests = [];
    const sent = [];
    let chats = 0;
    const docClient = createDocClient({
      baseUrl: 'http://docs.test',
      apiKey: 'test-doc-key',
      fetchImpl: async (url, options) => {
        requests.push({ url, options });
        return Response.json({ results: configured ? [hit] : [] });
      },
    });
    const ctx = app(
      {
        chat: async ({ messages }) => {
          if (chats++ === 0) {
            // Model-supplied destination fields must never replace Discord's.
            return use('search_documents', {
              query: 'test document',
              guildId: '999',
              channelId: threadId,
              onBehalfOf: principal.person.id,
            });
          }
          assert.equal(messages.at(-1).content[0].content.results.length, configured ? 1 : 0);
          return answer(configured ? 'Here is the document.' : 'No document matches.');
        },
      },
      docClient,
    );
    let message;
    const thread = {
      id: threadId,
      parentId,
      isThread: () => true,
      sendTyping: async () => {},
      send: async (part) => sent.push(part),
      messages: { fetch: async () => new Map([[message.id, message]]) },
    };
    message = {
      id: '323456789012345679',
      author: { id: 'member-1' },
      content: '<@bot-1> Read the document',
      channel: thread,
      guildId,
    };
    await handleMention(message, { appContext: ctx, botId: 'bot-1' });
    assert.equal(chats, 2);
    assert.equal(requests.length, configured ? 4 : 1);
    for (const { url, options } of requests) {
      assert.equal(url, 'http://docs.test/search');
      assert.equal(options.headers['X-Guild-ID'], guildId);
      assert.equal(options.headers['X-Channel-ID'], parentId);
      assert.equal(options.headers['X-On-Behalf-Of'], undefined);
      assert.deepEqual(JSON.parse(options.body), { query: 'test document', k: 10 });
    }
    if (configured) {
      assert.match(sent.join('\n'), /Here is the document/);
      assert.ok(sent.join('\n').includes(hit.url));
    } else {
      assert.deepEqual(sent, ['No document matches.']);
      assert.equal(sent.join('\n').includes(hit.url), false);
    }
  });
}
