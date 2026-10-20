import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MessageFlags } from 'discord.js';
import {
  chunkForDiscord,
  discordMessageParts,
  limitEmbeds,
  sendDiscordMessage,
  DELIVERY_ERROR,
} from '../src/adapters/discord/messages.js';
import { safeReply } from '../src/adapters/discord/interactions.js';
import { makeChannelNotifier } from '../src/adapters/discord/meetingPosts.js';
import { handleRecordInteraction } from '../src/adapters/discord/recording.js';

test('text at the limit stays intact; long lines and emoji split without data loss', () => {
  for (const text of [
    'x'.repeat(2000),
    'x'.repeat(1999) + '\n',
    'x'.repeat(4500),
    'x' + '😀'.repeat(2500),
  ]) {
    const chunks = chunkForDiscord(text);
    assert.equal(chunks.join(''), text);
    assert.ok(chunks.every((part) => part.length <= 2000 && part.isWellFormed()));
  }
  assert.deepEqual(chunkForDiscord('a'.repeat(1500) + '\n' + 'b'.repeat(1500)), [
    'a'.repeat(1500),
    'b'.repeat(1500),
  ]);
});

test('a story paragraph splits at a sentence ending before falling back to spaces', () => {
  const sentence = 'The explorers decided to REMAIN beside the river. ';
  const text = sentence + 'The journey continued '.repeat(150);
  const chunks = chunkForDiscord(text);
  assert.equal(chunks[0], sentence);
  assert.equal(chunks.join(''), text);
  assert.ok(chunks.every((part) => part.length <= 2000));
});

test('paragraph breaks take priority over later sentence endings', () => {
  for (const separator of ['\n\n', '\r\n\r\n', '\n \n']) {
    const paragraph = 'The first chapter ended here.' + separator;
    const chunks = chunkForDiscord(paragraph + 'The next chapter began. ' + 'word '.repeat(500));
    // The splitter consumes the paragraph separator and removes its final LF.
    assert.equal(chunks[0], paragraph.slice(0, -1));
    assert.ok(chunks[1].startsWith('The next chapter began. '));
    assert.ok(chunks.every((part) => part.length <= 2000));
  }
});

test('Markdown section starts take priority over the paragraph break after a heading', () => {
  for (let level = 1; level <= 6; level += 1) {
    const intro = 'intro '.repeat(200) + '\n\n';
    const heading = '#'.repeat(level) + ' Next chapter\n\n';
    const text = intro + heading + 'The journey continued '.repeat(200);
    const chunks = chunkForDiscord(text);
    assert.equal(chunks[0], intro.slice(0, -1));
    assert.ok(chunks[1].startsWith(heading + 'The journey continued '));
    assert.equal(chunks[0] + '\n' + chunks.slice(1).join(''), text);
    assert.ok(chunks.every((part) => part.length <= 2000));
  }
});

test('a heading at the start stays with its body when that section exceeds the limit', () => {
  const heading = '# The journey begins.\n\n';
  const text = heading + 'word '.repeat(1000);
  const chunks = chunkForDiscord(text);
  assert.ok(chunks[0].startsWith(heading + 'word '));
  assert.equal(chunks.join(''), text);
  assert.ok(chunks.every((part) => part.length <= 2000));
});

test('the last Markdown section boundary is used when several headings fit', () => {
  const previous = '# First\n\nIntroduction\n\n## Second\n\nMore text\n\n';
  const heading = '### Third\n\n';
  const chunks = chunkForDiscord(previous + heading + 'word '.repeat(600));
  assert.equal(chunks[0], previous.slice(0, -1));
  assert.ok(chunks[1].startsWith(heading + 'word '));
});

test('heading-like lines in fenced code do not create Markdown section boundaries', () => {
  const intro = 'intro '.repeat(200) + '\n';
  const chunks = chunkForDiscord(intro + '```md\n# Example\n\n' + 'word '.repeat(500) + '\n```');
  assert.ok(chunks[0].startsWith(intro + '```md\n# Example'));
  assert.ok(chunks.every((part) => part.length <= 2000));
  assert.ok(chunks.every((part) => (part.match(/```/g) ?? []).length % 2 === 0));
});

test('whitespace keeps REMAIN intact when the character limit falls inside the word', () => {
  for (const separator of [' ', '\t']) {
    const lead = ('word' + separator).repeat(399);
    const text = lead + 'REMAIN' + separator + 'word '.repeat(500);
    const chunks = chunkForDiscord(text);
    assert.equal(chunks[0], lead);
    assert.ok(chunks[1].startsWith('REMAIN' + separator));
    assert.equal(chunks.join(''), text);
    assert.ok(chunks.every((part) => part.length <= 2000));
  }
});

test('sentence boundaries include closing quotation marks', () => {
  const sentence = 'She whispered, “We will remain.” ';
  const chunks = chunkForDiscord(sentence + 'word '.repeat(500));
  assert.equal(chunks[0], sentence);
});

test('code fences close and reopen with their language, including near the limit', () => {
  for (const lead of ['', 'intro\n', 'x'.repeat(1998) + '\n']) {
    const text = lead + '```js\n' + 'console.log(1);\n'.repeat(400) + '```\nafter';
    const chunks = chunkForDiscord(text);
    assert.ok(chunks.every((part) => part.length <= 2000));
    assert.ok(chunks.every((part) => (part.match(/```/g) ?? []).length % 2 === 0));
    assert.equal(
      chunks.join('\n').replace(/```js\n|```\n?/g, ''),
      text.replace(/```js\n|```\n?/g, ''),
    );
    assert.ok(chunks.slice(1, -1).every((part) => part.startsWith('```js\n')));
  }
  const chunks = chunkForDiscord('```python\n' + 'x'.repeat(5000) + '\n```');
  assert.ok(
    chunks.every(
      (part) => part.length <= 2000 && part.startsWith('```python\n') && part.endsWith('```'),
    ),
  );
});

function interaction({ deferred = false, replied = false, ephemeral = true } = {}) {
  const calls = [];
  return {
    calls,
    deferred,
    replied,
    ephemeral,
    async reply(payload) {
      calls.push(['reply', payload]);
      this.replied = true;
    },
    async editReply(payload) {
      calls.push(['editReply', payload]);
      this.replied = true;
    },
    async followUp(payload) {
      calls.push(['followUp', payload]);
    },
    async deleteReply() {
      calls.push(['deleteReply']);
    },
  };
}

for (const state of [{}, { deferred: true }, { replied: true }]) {
  test(`long private reply uses follow-ups with privacy intact: ${JSON.stringify(state)}`, async () => {
    const i = interaction(state);
    await safeReply(i, {
      content: 'x'.repeat(4500),
      ephemeral: true,
      allowedMentions: { parse: [] },
    });
    assert.deepEqual(
      i.calls.map(([method]) => method),
      [state.deferred ? 'editReply' : state.replied ? 'followUp' : 'reply', 'followUp', 'followUp'],
    );
    assert.equal(i.calls.map(([, p]) => p.content).join(''), 'x'.repeat(4500));
    for (const [method, p] of i.calls) {
      assert.ok(p.content.length <= 2000);
      assert.deepEqual(p.allowedMentions, { parse: [] });
      assert.equal(p.flags, method === 'editReply' ? undefined : MessageFlags.Ephemeral);
    }
  });
}

test('deferred visibility wins over a conflicting payload hint', async () => {
  for (const ephemeral of [true, false]) {
    const i = interaction({ deferred: true, ephemeral });
    await safeReply(i, { content: 'x'.repeat(2500), ephemeral: !ephemeral });
    assert.equal(i.calls[1][1].flags, ephemeral ? MessageFlags.Ephemeral : undefined);
  }
});

test('embed property, field count, embed count and shared text limits are enforced without mutation', () => {
  const source = Array.from({ length: 12 }, () => ({
    title: 't'.repeat(300),
    description: 'd'.repeat(5000),
    author: { name: 'a'.repeat(300) },
    footer: { text: 'f'.repeat(3000) },
    fields: Array.from({ length: 30 }, () => ({ name: 'n'.repeat(300), value: 'v'.repeat(2000) })),
  }));
  const original = structuredClone(source);
  const embeds = limitEmbeds(source);
  let total = 0;
  for (const embed of embeds) {
    for (const [text, max] of [
      [embed.title, 256],
      [embed.description, 4096],
      [embed.author?.name, 256],
      [embed.footer?.text, 2048],
      ...(embed.fields ?? []).flatMap((f) => [
        [f.name, 256],
        [f.value, 1024],
      ]),
    ]) {
      assert.ok((text?.length ?? 0) <= max);
      total += text?.length ?? 0;
    }
    assert.ok((embed.fields?.length ?? 0) <= 25);
  }
  assert.ok(total <= 6000);
  assert.ok(embeds.length <= 10 && embeds.length > 0);
  assert.match(JSON.stringify(embeds), /…/);
  assert.deepEqual(source, original);
  const fields = limitEmbeds([{ fields: [{ name: 'name', value: 'v'.repeat(2000) }] }])[0].fields;
  assert.equal(fields[0].value.length, 1024);
  assert.ok(fields[0].value.endsWith('…'));
  const countLimited = limitEmbeds([
    { fields: Array.from({ length: 30 }, () => ({ name: 'n', value: 'v' })) },
  ]);
  assert.equal(countLimited[0].fields.length, 25);
  assert.ok(countLimited[0].fields.at(-1).value.endsWith('…'));
});

test('files and embeds stay on the first message only; embed-only payloads work', () => {
  const embeds = [{ title: 'hi' }];
  const files = [{ attachment: 'file' }];
  const parts = discordMessageParts({ content: 'x'.repeat(2500), embeds, files });
  assert.deepEqual(parts[0].embeds, embeds);
  assert.deepEqual(parts[0].files, files);
  assert.equal(parts[1].embeds, undefined);
  assert.equal(parts[1].files, undefined);
  assert.deepEqual(discordMessageParts({ embeds }), [{ embeds }]);
});

test('failed deferred delivery replaces the spinner with a short error without logging payloads', async (t) => {
  const logs = [];
  t.mock.method(console, 'error', (...args) => logs.push(args));
  const i = interaction({ deferred: true });
  let attempts = 0;
  const edit = i.editReply;
  i.editReply = async (payload) => {
    if (++attempts === 1) throw new Error('SECRET REPLY');
    await edit.call(i, payload);
  };
  await safeReply(i, { content: 'secret', ephemeral: true });
  assert.equal(i.calls[0][1].content, DELIVERY_ERROR);
  assert.deepEqual(logs, [['Could not deliver Discord reply']]);
});

test('failed continuation sends a private error; irrecoverable deferred failure clears spinner', async (t) => {
  t.mock.method(console, 'error', () => {});
  const i = interaction({ deferred: true });
  let attempts = 0;
  const followUp = i.followUp;
  i.followUp = async (payload) => {
    if (++attempts === 1) throw new Error('rejected');
    await followUp.call(i, payload);
  };
  await safeReply(i, { content: 'x'.repeat(2500), ephemeral: true });
  assert.equal(i.calls.at(-1)[1].content, DELIVERY_ERROR);
  assert.equal(i.calls.at(-1)[1].flags, MessageFlags.Ephemeral);
  const unavailable = interaction({ deferred: true });
  unavailable.editReply = async () => {
    throw new Error('unavailable');
  };
  await safeReply(unavailable, { content: 'reply' });
  assert.deepEqual(unavailable.calls, [['deleteReply']]);
});

test('channel notices and recording replies split through shared delivery', async () => {
  const sent = [];
  await makeChannelNotifier()({
    channel: { send: async (p) => sent.push(p) },
    content: 'x'.repeat(4500),
  });
  assert.equal(sent.map((p) => p.content).join(''), 'x'.repeat(4500));
  assert.ok(sent.every((p) => p.content.length <= 2000));
  const i = interaction();
  i.guildId = 'guild';
  i.options = { getSubcommand: () => 'status' };
  i.deferReply = async () => {
    i.deferred = true;
  };
  await handleRecordInteraction(
    i,
    {
      meetingSurface: {
        status: () => ({ status: 'recording', elapsedMs: 1000 }),
        activeSession: () => ({ voiceChannel: { name: 'x'.repeat(4500) } }),
      },
    },
    { subcommands: [{ name: 'status', auth: 'public' }] },
  );
  assert.ok(i.calls.length > 1);
  assert.ok(i.calls.every(([, p]) => p.content.length <= 2000));
  assert.ok(i.calls.slice(1).every(([, p]) => p.flags === MessageFlags.Ephemeral));
});

test('channel delivery reports failure and preserves the error for attachment callers', async () => {
  const sent = [];
  const error = new Error('failed');
  await assert.rejects(
    sendDiscordMessage(
      async (p) => {
        sent.push(p);
        if (sent.length === 1) throw error;
      },
      { content: 'long reply' },
    ),
    (e) => e === error,
  );
  assert.equal(sent[1].content, DELIVERY_ERROR);
});
