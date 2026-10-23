import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildServer } from '../src/web/server.js';
import { meetingDeliveryScenarios } from '../src/web/meetingDeliveryPreview.js';

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
