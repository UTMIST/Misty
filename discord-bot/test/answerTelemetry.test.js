import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { handleDirectMessage } from '../src/adapters/discord/mentions.js';
import { createToolExecutor } from '../src/toolExecutor.js';
import { createLlmClient } from '../src/clients/llmClient.js';

const cases = [
  'full',
  'partial',
  'withheld',
  'denied',
  'rate_limited',
  'timeout',
  'cancelled',
  'failed',
];
async function attempt(kind) {
  let sends = 0;
  let rounds = 0;
  const content = 'secret-answer-'.repeat(210);
  const message = {
    id: 'private-message',
    author: { id: 'private-person' },
    content: 'private-question',
    reply: async () => {},
    channel: {
      messages: { fetch: async () => [message] },
      sendTyping: async () => {},
      send: async () => {
        if (++sends === 2 && kind === 'partial') throw new Error('private-provider-error');
      },
    },
  };
  const appContext = {
    helperRequestLimiter: { tryConsume: () => ({ allowed: kind !== 'rate_limited' }) },
    directory: {
      getPersonByDiscordId: async () =>
        kind === 'denied' ? null : { id: 'private-person', active: true },
    },
    helperToolsEnabled: true,
    helperService: {
      prepare: async () => ({ messages: [{ role: 'user', content: 'private-question' }] }),
    },
    toolExecutor: createToolExecutor({
      llmClient: createLlmClient({
        baseUrl: 'https://fixture.invalid',
        apiKey: 'private-token',
        fetchImpl: async (_url, options) => {
          assert.ok(options.headers.traceparent || process.env.TELEMETRY_ENABLED === 'false');
          if (kind === 'timeout') return new Response(null, { status: 504 });
          if (kind === 'failed') throw new Error('private-provider-error');
          if (kind === 'cancelled') {
            const e = new Error('private-cancel');
            e.kind = 'cancelled';
            throw e;
          }
          const first = rounds++ === 0;
          return Response.json({
            usage: { output_tokens: 1 },
            content: first ? '' : content,
            stop_reason: first ? 'tool_use' : 'end_turn',
            content_blocks: first
              ? [{ type: 'tool_use', name: 'research', id: 'private-operation', input: {} }]
              : [],
          });
        },
      }),
      registry: {
        definitions: [],
        handlers: new Map([
          [
            'research',
            async () => ({
              content: 'private-result',
              revalidate: async () => kind !== 'withheld',
            }),
          ],
        ]),
      },
    }),
  };
  // A cancelled adapter request carries the AbortSignal distinction at the client boundary.
  if (kind === 'cancelled') {
    const client = createLlmClient({
      baseUrl: 'https://fixture.invalid',
      apiKey: 'private-token',
      fetchImpl: async () => {
        throw new Error('private-cancel');
      },
    });
    appContext.helperToolsEnabled = false;
    appContext.helperService.answer = () =>
      client.chat({ messages: [], signal: AbortSignal.abort() });
  }
  await handleDirectMessage(message, { appContext, botId: 'private-bot' });
}

if (process.argv[2] === '--emit') {
  for (const kind of cases) await attempt(kind);
} else {
  test('Discord admission, tool rounds and acknowledged multipart delivery stay content-free', () => {
    for (const enabled of ['true', 'false']) {
      const run = spawnSync(process.execPath, [fileURLToPath(import.meta.url), '--emit'], {
        env: {
          ...process.env,
          TELEMETRY_ENV: 'local',
          TELEMETRY_TRAFFIC: 'synthetic',
          TELEMETRY_ENABLED: enabled,
        },
        encoding: 'utf8',
      });
      assert.equal(run.status, 0, run.stderr);
      assert.doesNotMatch(run.stdout + run.stderr, /private-|secret-answer/);
      const events = run.stdout
        .trim()
        .split('\n')
        .filter(Boolean)
        .map((x) => JSON.parse(x));
      if (enabled === 'false') {
        assert.equal(events.length, 0);
        continue;
      }
      const ends = events.filter((e) => e.operation === 'answer' && e.event_name === 'end');
      assert.deepEqual(
        ends.map((e) => e.result),
        cases,
      );
      for (const end of ends) {
        const trace = events.filter((e) => e.trace_id === end.trace_id);
        assert.equal(
          trace.filter((e) => e.event_name === 'admitted').length,
          ['denied', 'rate_limited'].includes(end.result) ? 0 : 1,
        );
        assert.equal(
          trace.filter((e) => e.event_name === 'part_sent').length,
          end.result === 'full' ? 2 : end.result === 'partial' ? 1 : 0,
        );
        if (['timeout', 'cancelled'].includes(end.result)) {
          assert.equal(
            trace.find((e) => e.operation === 'answer.model' && e.event_name === 'end')?.result,
            end.result,
          );
        }
        if (['full', 'partial', 'withheld'].includes(end.result)) {
          assert.equal(trace.filter((e) => e.event_name === 'tool_round').length, 1);
          assert.equal(
            trace.filter((e) => e.operation === 'answer.tools' && e.event_name === 'end').length,
            1,
          );
          assert.equal(
            trace.filter((e) => e.operation === 'answer.model' && e.event_name === 'start').length,
            end.result === 'withheld' ? 1 : 2,
          );
        }
        for (const child of trace.filter((e) => e.operation !== 'answer'))
          assert.ok(trace.some((e) => e.span_id === child.parent_span_id));
      }
    }
  });
}
