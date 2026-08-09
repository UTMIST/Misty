import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeAttachmentPoster } from '../src/adapters/discord.js';

function fakeChannel({ sendImpl } = {}) {
  const calls = [];
  return {
    calls,
    send:
      sendImpl ??
      (async (payload) => {
        calls.push(payload);
        return payload;
      }),
  };
}

test('poster posts a PDF attachment decoded from pdf_b64', async () => {
  const poster = makeAttachmentPoster();
  const pdfBytes = Buffer.from('%PDF-fake-content');
  const channel = fakeChannel();
  await poster({ channel, report: { pdf_b64: pdfBytes.toString('base64') } });

  assert.equal(channel.calls.length, 1);
  const { files } = channel.calls[0];
  assert.equal(files.length, 1);
  assert.equal(files[0].name, 'meeting-minutes.pdf');
  assert.ok(Buffer.from(files[0].attachment).equals(pdfBytes));
});

test('poster never attaches audio, even if the report still carries some', async () => {
  const poster = makeAttachmentPoster();
  const pdfBytes = Buffer.from('pdf-bytes');
  const channel = fakeChannel();
  await poster({
    channel,
    report: {
      pdf_b64: pdfBytes.toString('base64'),
      audio_b64: Buffer.from('audio-bytes').toString('base64'),
    },
  });

  const { files } = channel.calls[0];
  assert.equal(files.length, 1);
  assert.equal(files[0].name, 'meeting-minutes.pdf');
});

test('poster mentions the member who started the recording', async () => {
  const poster = makeAttachmentPoster();
  const channel = fakeChannel();
  await poster({
    channel,
    report: { pdf_b64: Buffer.from('pdf').toString('base64') },
    requesterId: '424242',
  });

  assert.equal(channel.calls.length, 1);
  assert.match(channel.calls[0].content, /<@424242>/);
});

test('poster posts without a mention when the requester is unknown', async () => {
  const poster = makeAttachmentPoster();
  const channel = fakeChannel();
  await poster({ channel, report: { pdf_b64: Buffer.from('pdf').toString('base64') } });

  assert.equal(channel.calls.length, 1);
  assert.doesNotMatch(channel.calls[0].content, /<@/);
});

test('a failed send surfaces instead of being swallowed', async () => {
  // The service destroys the session as soon as it responds, so a failed send
  // means the minutes are gone permanently. Swallowing it told the user
  // "minutes will post here shortly" for a meeting that no longer exists.
  const poster = makeAttachmentPoster();
  const channel = {
    send: async () => {
      throw new Error('missing permissions');
    },
  };
  const originalError = console.error;
  console.error = () => {};
  try {
    await assert.rejects(
      () => poster({ channel, report: { pdf_b64: Buffer.from('x').toString('base64') } }),
      /missing permissions/,
    );
  } finally {
    console.error = originalError;
  }
});

test('a malformed report surfaces too', async () => {
  // Same reasoning: no PDF reaches the channel, so /record stop must not report
  // success.
  const poster = makeAttachmentPoster();
  const channel = fakeChannel();
  const originalError = console.error;
  console.error = () => {};
  try {
    await assert.rejects(() => poster({ channel, report: {} }));
    assert.equal(channel.calls.length, 0);
  } finally {
    console.error = originalError;
  }
});
