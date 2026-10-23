import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { buildServer } from '../src/web/server.js';
import { meetingDeliveryScenarios } from '../src/web/meetingDeliveryPreview.js';

test('preview needs no acting identity while ordinary commands and pending requests stay gated', () => {
  const submit = { dataset: {}, textContent: 'Run simulation ↗' };
  const scenario = {};
  const identityNote = {};
  const elements = {
    'form-strip': {
      querySelectorAll: (selector) =>
        selector === 'button[type="submit"]' ? [submit] : [scenario],
    },
    'form-identity-note': identityNote,
  };
  const context = createContext({
    localStorage: { getItem: () => '' },
    document: { getElementById: (id) => (elements[id] ??= {}) },
  });
  const source = readFileSync(new URL('../src/web/public/app.js', import.meta.url), 'utf8');
  // Load the real page state and functions without imports or browser startup.
  const start = source.indexOf('\nconst state =');
  const end = source.indexOf('\nactingAsInput.value =');
  assert.ok(start >= 0 && end > start);
  runInContext(source.slice(start, end), context);

  for (const [selectedKey, actingAs, pending, disabled, hint] of [
    ['meeting-delivery', '', false, false, /no identity/i],
    ['meeting-delivery', '', true, true, /in progress/i],
    ['meeting-delivery', '123', false, false, /no identity/i],
    ['helper-dm', '', false, true, /choose an identity/i],
    ['helper-dm', '123', false, false, /selected identity/i],
    ['helper-dm', '123', true, true, /in progress/i],
  ]) {
    context.formState = { selectedKey, actingAs, pending };
    runInContext('Object.assign(state, formState); updateSubmitState();', context);
    assert.equal(submit.disabled, disabled);
    assert.equal(scenario.disabled, pending);
    assert.match(identityNote.textContent, hint);
  }
});

test('playground runs stop, auto-stop, retry and failure scenarios without external clients', async () => {
  const server = await buildServer({ commands: new Map(), appContext: {} });
  try {
    for (const scenario of meetingDeliveryScenarios) {
      const response = await server.inject({
        method: 'POST',
        url: '/api/meeting-delivery/preview',
        payload: { scenario },
      });
      assert.equal(response.statusCode, 200);
      const result = response.json();
      assert.equal(result.simulated, true);
      assert.equal(result.operations.filter((op) => op.startsWith('Finalize once')).length, 1);
      const links = result.messages.filter((message) => message.id === 'notification-1');
      if (['explicit', 'automatic', 'retry', 'admin-only'].includes(scenario)) {
        assert.equal(result.delivery_status, 'posted');
        assert.equal(links.length, 1);
        assert.match(links[0].content, /<@100000000000000000>/);
        assert.match(links[0].content, /minutes and the full transcript/);
      } else {
        assert.equal(links.length, 0);
        assert.equal(result.delivery_status, scenario === 'delivery-failed' ? 'failed' : 'pending');
      }
      if (scenario === 'delivery-failed') assert.equal(result.persistence_status, 'saved');
      if (scenario === 'persistence-failed') assert.equal(result.persistence_status, 'failed');
    }
    const invalid = await server.inject({
      method: 'POST',
      url: '/api/meeting-delivery/preview',
      payload: { scenario: 'real-discord' },
    });
    assert.equal(invalid.statusCode, 400);
  } finally {
    await server.close();
  }
});
