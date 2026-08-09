import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createMeetingClient, MeetingUnavailable } from '../src/meeting/meetingClient.js';

const BASE = 'http://meeting';
const WS_BASE = 'ws://meeting';
const KEY = 'botkey';

function fakeFetch(responses) {
  const calls = [];
  let i = 0;
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    const r = responses[i++];
    if (r.throw) throw new Error('network down');
    return {
      status: r.status,
      ok: r.ok ?? (r.status >= 200 && r.status < 300),
      json: async () => {
        if (r.badJson) throw new Error('bad json');
        return r.body;
      },
    };
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = FakeWebSocket.CONNECTING;
    this.listeners = {};
    FakeWebSocket.instances.push(this);
  }
  addEventListener(event, cb) {
    (this.listeners[event] ||= []).push(cb);
  }
  on(event, cb) {
    (this.listeners[event] ||= []).push(cb);
  }
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.closed = true;
  }
  _open() {
    this.readyState = FakeWebSocket.OPEN;
    for (const cb of this.listeners.open || []) cb();
  }
  _error(err) {
    for (const cb of this.listeners.error || []) cb(err);
  }
  _close() {
    this.readyState = FakeWebSocket.CLOSED;
    for (const cb of this.listeners.close || []) cb();
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

test('encodeFrame produces exact byte layout: len, speakerId utf8, 8-byte BE ts, opus payload', () => {
  const { encodeFrame } = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY });
  const frame = encodeFrame('u1', 500, Buffer.from([1, 2, 3]));
  assert.deepEqual(
    [...frame],
    [0x00, 0x02, 0x75, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0xf4, 0x01, 0x02, 0x03],
  );
});

test('encodeFrame uses byte length (not char length) for multi-byte utf8 speaker ids', () => {
  const { encodeFrame } = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY });
  // '☺' (U+263A) is 1 char but 3 bytes in utf8
  const speakerId = '☺';
  const idBytes = Buffer.from(speakerId, 'utf8');
  assert.equal(idBytes.length, 3);
  const frame = encodeFrame(speakerId, 0, Buffer.alloc(0));
  assert.equal(frame.readUInt16BE(0), 3);
  assert.deepEqual(frame.subarray(2, 5), idBytes);
  assert.equal(frame.length, 2 + 3 + 8 + 0);
});

test('getTranscript GETs the transcript endpoint with X-API-Key and parses JSON', async () => {
  const fetchImpl = fakeFetch([
    { status: 200, body: { segments: [{ speaker: 'a', text: 'hi' }] } },
  ]);
  const client = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY, fetchImpl });
  const res = await client.getTranscript('sess-1');
  assert.deepEqual(res, { segments: [{ speaker: 'a', text: 'hi' }] });
  const call = fetchImpl.calls[0];
  assert.equal(call.url, `${BASE}/meetings/sess-1/transcript`);
  assert.equal(call.opts.headers['X-API-Key'], KEY);
});

test('getTranscript throws MeetingUnavailable on non-ok response', async () => {
  const fetchImpl = fakeFetch([{ status: 503, body: {} }]);
  const client = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.getTranscript('sess-1'), MeetingUnavailable);
});

test('getTranscript throws MeetingUnavailable when fetch throws', async () => {
  const fetchImpl = fakeFetch([{ throw: true }]);
  const client = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.getTranscript('sess-1'), MeetingUnavailable);
});

test('stop POSTs the stop endpoint with X-API-Key and returns parsed JSON', async () => {
  const body = { transcript: 't', minutes: 'm', pdf_b64: 'p' };
  const fetchImpl = fakeFetch([{ status: 200, body }]);
  const client = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY, fetchImpl });
  const res = await client.stop('sess-1');
  assert.deepEqual(res, body);
  const call = fetchImpl.calls[0];
  assert.equal(call.url, `${BASE}/meetings/sess-1/stop`);
  assert.equal(call.opts.method, 'POST');
  assert.equal(call.opts.headers['X-API-Key'], KEY);
});

test('stop throws MeetingUnavailable on non-ok response and on transport error', async () => {
  const fetchImpl = fakeFetch([{ status: 500, body: {} }]);
  const client = createMeetingClient({ baseUrl: BASE, wsUrl: WS_BASE, apiKey: KEY, fetchImpl });
  await assert.rejects(() => client.stop('sess-1'), MeetingUnavailable);

  const fetchImpl2 = fakeFetch([{ throw: true }]);
  const client2 = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    fetchImpl: fetchImpl2,
  });
  await assert.rejects(() => client2.stop('sess-1'), MeetingUnavailable);
});

test('openStream connects to a URL with no key query param, only guild_id (URL-encoded)', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  client.openStream('sess-1', { guildId: 'g 1&x' });
  const ws = FakeWebSocket.instances.at(-1);
  assert.equal(ws.url, `${WS_BASE}/meetings/sess-1/stream?guild_id=${encodeURIComponent('g 1&x')}`);
  assert.doesNotMatch(ws.url, /key=/);
});

test('on open, the first frame sent is the {"key": ...} auth text frame', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  ws._open();
  assert.equal(ws.sent.length, 1);
  assert.equal(ws.sent[0], JSON.stringify({ key: KEY }));
});

test('sendControl sends a JSON text frame with speaker_id and display_name, after the auth frame', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  ws._open();
  stream.sendControl({ speakerId: 'u1', displayName: 'Alice' });
  assert.equal(ws.sent.length, 2);
  assert.equal(ws.sent[0], JSON.stringify({ key: KEY }));
  assert.equal(ws.sent[1], JSON.stringify({ speaker_id: 'u1', display_name: 'Alice' }));
});

test('sendFrame sends encodeFrame bytes as a binary frame, after the auth frame', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  ws._open();
  const opus = Buffer.from([9, 9, 9]);
  stream.sendFrame('u1', 500, opus);
  assert.equal(ws.sent.length, 2);
  assert.equal(ws.sent[0], JSON.stringify({ key: KEY }));
  const expected = client.encodeFrame('u1', 500, opus);
  assert.deepEqual(Buffer.from(ws.sent[1]), expected);
});

test('sendFrame/sendControl before the socket is open are queued and flushed on open, after the auth frame', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  const opus = Buffer.from([1, 2]);
  stream.sendControl({ speakerId: 'u1', displayName: 'Alice' });
  stream.sendFrame('u1', 1, opus);
  assert.equal(ws.sent.length, 0); // not open yet, queued
  ws._open();
  assert.equal(ws.sent.length, 3);
  assert.equal(ws.sent[0], JSON.stringify({ key: KEY }));
  assert.equal(ws.sent[1], JSON.stringify({ speaker_id: 'u1', display_name: 'Alice' }));
  assert.deepEqual(Buffer.from(ws.sent[2]), client.encodeFrame('u1', 1, opus));
});

test('close() closes the underlying socket', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  stream.close();
  assert.equal(ws.closed, true);
});

test('a WS "error" event does not throw out of openStream/sendFrame and invokes onError', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const onErrorCalls = [];
  const stream = client.openStream('sess-1', {
    guildId: 'g1',
    onError: (e) => onErrorCalls.push(e),
  });
  const ws = FakeWebSocket.instances.at(-1);

  const originalError = console.error;
  console.error = () => {};
  let threw = false;
  try {
    ws._error(new Error('ECONNREFUSED'));
  } catch {
    threw = true;
  } finally {
    console.error = originalError;
  }
  assert.equal(threw, false);
  assert.equal(onErrorCalls.length, 1);
  assert.equal(onErrorCalls[0].message, 'ECONNREFUSED');

  // dead after error: sendFrame/sendControl must no-op, not throw
  assert.doesNotThrow(() => stream.sendFrame('u1', 1, Buffer.from([1])));
  assert.doesNotThrow(() => stream.sendControl({ speakerId: 'u1', displayName: 'Alice' }));
  assert.equal(ws.sent.length, 0);
});

test('a WS "close" event marks the stream dead so subsequent sends are dropped, not queued', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);

  ws._close();
  assert.doesNotThrow(() => stream.sendFrame('u1', 1, Buffer.from([1])));
  assert.equal(ws.sent.length, 0);
});

test('openStream works without an onError callback (optional param)', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);

  const originalError = console.error;
  console.error = () => {};
  try {
    assert.doesNotThrow(() => ws._error(new Error('boom')));
  } finally {
    console.error = originalError;
  }
});

test('endAudio sends the end-of-audio control frame after the audio', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  ws._open();
  stream.sendFrame('u1', 0, Buffer.from([1, 2, 3]));
  stream.endAudio();

  // Ordering is the whole point: the signal must be the LAST thing on the wire,
  // so the server can treat it as proof all audio has arrived.
  assert.equal(ws.sent.at(-1), JSON.stringify({ end_of_audio: true }));
});

test('endAudio is queued like any other frame if the socket is not open yet', () => {
  const client = createMeetingClient({
    baseUrl: BASE,
    wsUrl: WS_BASE,
    apiKey: KEY,
    WebSocketImpl: FakeWebSocket,
  });
  const stream = client.openStream('sess-1', { guildId: 'g1' });
  const ws = FakeWebSocket.instances.at(-1);
  stream.endAudio();
  ws._open();

  assert.equal(ws.sent.at(-1), JSON.stringify({ end_of_audio: true }));
});
