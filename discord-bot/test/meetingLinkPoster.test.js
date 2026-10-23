import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MessageFlags, PermissionFlagsBits, PermissionsBitField } from 'discord.js';
import { makeMeetingLinkPoster } from '../src/adapters/discord/meetingPosts.js';
import { createMeetingDelivery } from '../src/meeting/meetingDelivery.js';

function setup(overrides = {}) {
  const record = {
    meeting_id: '22300000-0000-4000-8000-000000000001',
    guild_id: '1',
    text_channel_id: '2',
    requester_discord_id: '4',
    google_document_id: 'google-id',
    doc_url: 'https://docs.google.com/document/d/google-id/edit',
    title: 'Team **sync** @everyone <@99>',
    started_at: '2026-10-22T18:00:00Z',
    audience_team_ids: ['team'],
    persistence_status: 'saved',
    delivery_status: 'pending',
    summary: 'PRIVATE SUMMARY',
    decisions: ['PRIVATE DECISION'],
    transcript: 'PRIVATE TRANSCRIPT',
    ...overrides,
  };
  const messages = new Map();
  const sends = [];
  const edits = [];
  const member = { id: 'bot' };
  const permissions = new PermissionsBitField([
    PermissionFlagsBits.ViewChannel,
    PermissionFlagsBits.ReadMessageHistory,
  ]);
  const channel = {
    id: '2',
    guildId: '1',
    guild: {
      members: {
        fetchMe: async (options) => {
          assert.deepEqual(options, { force: true });
          return member;
        },
      },
    },
    permissionsFor: (resolvedMember) => {
      assert.equal(resolvedMember, member);
      return permissions;
    },
    isTextBased: () => true,
    messages: {
      fetch: async (options) => {
        if (options.message) {
          if (messages.has(options.message)) return messages.get(options.message);
          throw Object.assign(new Error('unknown message'), { code: 10008 });
        }
        return { find: (predicate) => [...messages.values()].find(predicate) };
      },
    },
    send: async (payload) => {
      sends.push(payload);
      const message = {
        id: String(100 + sends.length),
        author: { id: 'bot' },
        content: payload.content,
        edit: async (updated) => edits.push(updated),
      };
      messages.set(message.id, message);
      return message;
    },
  };
  const destinations = [];
  const client = {
    user: { id: 'bot' },
    channels: {
      fetch: async (id, options) => {
        destinations.push({ id, options });
        return channel;
      },
    },
  };
  return { record, client, channel, messages, sends, edits, destinations, permissions };
}

test('untrusted empty history cannot authorize a send in a channel or thread', async () => {
  for (const thread of [false, true]) {
    for (const missing of [
      PermissionFlagsBits.ReadMessageHistory,
      PermissionFlagsBits.ViewChannel,
      null,
    ]) {
      const f = setup();
      f.channel.isThread = () => thread;
      if (missing === null) f.channel.permissionsFor = () => null;
      else f.permissions.remove(missing);
      // Model Discord's successful [] response rather than a 50013 exception.
      // This must never be interpreted as proof that no notification exists.
      let historyReads = 0;
      f.channel.messages.fetch = async () => {
        historyReads++;
        return new Map();
      };
      await assert.rejects(() => makeMeetingLinkPoster(f.client)(f.record), /history access/);
      assert.equal(historyReads, 0);
      assert.equal(f.sends.length, 0);
      assert.equal(f.edits.length, 0);
    }
  }
});

test('lost receipt plus restart with denied history fails delivery, then recovers the original message', async () => {
  const f = setup();
  let catalogDown = true;
  const records = {
    get: async () => ({ ...f.record }),
    list: async (status) => (f.record.delivery_status === status ? [f.record] : []),
    update: async (_id, status, messageId) => {
      if (catalogDown) throw new Error('lost receipt');
      f.record.delivery_status = status;
      f.record.discord_message_id = messageId;
      return { ...f.record };
    },
  };
  await createMeetingDelivery({ records, post: makeMeetingLinkPoster(f.client) }).deliver(
    f.record.meeting_id,
  );
  assert.equal(f.sends.length, 1);
  assert.equal(f.record.discord_message_id, undefined);

  catalogDown = false;
  f.permissions.remove(PermissionFlagsBits.ReadMessageHistory);
  const fetchHistory = f.channel.messages.fetch;
  f.channel.messages.fetch = async (options) =>
    f.permissions.has(PermissionFlagsBits.ReadMessageHistory) ? fetchHistory(options) : new Map();
  // A fresh service has no remembered receipt; the fake sender intentionally
  // ignores nonce deduplication to model retry after Discord's nonce expires.
  const restarted = createMeetingDelivery({ records, post: makeMeetingLinkPoster(f.client) });
  await restarted.poll();
  assert.equal(f.record.delivery_status, 'failed');
  assert.equal(f.record.persistence_status, 'saved');
  assert.equal(f.sends.length, 1);
  assert.equal(f.edits.length, 0);

  f.permissions.add(PermissionFlagsBits.ReadMessageHistory);
  await restarted.poll();
  assert.equal(f.record.delivery_status, 'posted');
  assert.equal(f.record.discord_message_id, '101');
  assert.equal(f.sends.length, 1);
  assert.equal(f.edits.length, 1);
  assert.deepEqual(f.edits[0].allowedMentions, { parse: [], users: [] });
});

test('saved link identifies title/time/requester without leaking content or granting access', async () => {
  const f = setup();
  await makeMeetingLinkPoster(f.client)(f.record);
  assert.deepEqual(f.destinations, [{ id: '2', options: { force: true } }]);
  const payload = f.sends[0];
  assert.match(payload.content, /<@4>/);
  assert.match(payload.content, /Team \\\*\\\*sync\\\*\\\*/);
  assert.match(payload.content, /<t:1792692000:f>/);
  assert.match(payload.content, /minutes and the full transcript/);
  assert.ok(payload.content.includes(f.record.doc_url));
  assert.match(payload.content, /own Drive access/);
  assert.doesNotMatch(payload.content, /PRIVATE/);
  assert.deepEqual(payload.allowedMentions, { parse: [], users: ['4'] });
  assert.equal(payload.flags, MessageFlags.SuppressEmbeds);
  assert.equal(payload.enforceNonce, true);
  assert.equal(payload.nonce.length, 25);
});

test('known receipt edits the original message without pinging the requester again', async () => {
  const f = setup();
  const post = makeMeetingLinkPoster(f.client);
  const id = await post(f.record);
  await post({ ...f.record, discord_message_id: id });
  assert.equal(f.sends.length, 1);
  assert.equal(f.edits.length, 1);
  assert.deepEqual(f.edits[0].allowedMentions, { parse: [], users: [] });
});

test('restart after an ambiguous send recovers recent notification without a catalog receipt', async () => {
  const f = setup();
  const send = f.channel.send;
  f.channel.send = async (payload) => {
    await send(payload);
    throw new Error('response lost after Discord accepted');
  };
  await assert.rejects(() => makeMeetingLinkPoster(f.client)(f.record));
  const id = await makeMeetingLinkPoster(f.client)(f.record);
  assert.equal(id, '101');
  assert.equal(f.sends.length, 1);
  assert.equal(f.edits.length, 1);
});

test('deleted notification can be replaced, but a forbidden fetch never becomes another send', async () => {
  const f = setup({ discord_message_id: 'deleted' });
  await makeMeetingLinkPoster(f.client)(f.record);
  assert.equal(f.sends.length, 1);
  f.channel.messages.fetch = async () => {
    throw Object.assign(new Error('forbidden'), { code: 50013 });
  };
  await assert.rejects(() => makeMeetingLinkPoster(f.client)(f.record));
  assert.equal(f.sends.length, 1);
});

test('private thread is fetched by exact ID; parent/wrong guild/unavailable channels fail closed', async () => {
  const f = setup();
  f.channel.parentId = 'broader-parent';
  f.channel.isThread = () => true;
  await makeMeetingLinkPoster(f.client)(f.record);
  for (const changes of [{ id: 'broader-parent' }, { guildId: 'other' }, { messages: null }]) {
    const other = setup();
    Object.assign(other.channel, changes);
    await assert.rejects(() => makeMeetingLinkPoster(other.client)(other.record));
    assert.equal(other.sends.length, 0);
  }
});

test('unsafe URL and incomplete/removed/disabled records never send', async () => {
  for (const overrides of [
    { doc_url: 'https://attacker.test' },
    { doc_url: 'https://docs.google.com/document/d/another/edit' },
    { persistence_status: 'shared' },
    { delivery_status: 'disabled' },
    { removed_at: 'now' },
  ]) {
    const f = setup(overrides);
    await assert.rejects(() => makeMeetingLinkPoster(f.client)(f.record));
    assert.equal(f.destinations.length, 0);
    assert.equal(f.sends.length, 0);
  }
});

test('admin-only and missing requester/title/time use explicit safe fallbacks', async () => {
  const f = setup({
    audience_team_ids: [],
    requester_discord_id: null,
    title: null,
    started_at: null,
  });
  await makeMeetingLinkPoster(f.client)(f.record);
  assert.match(f.sends[0].content, /admin-only/);
  assert.match(f.sends[0].content, /\*\*Meeting\*\*/);
  assert.doesNotMatch(f.sends[0].content, /<@|<t:/);
});
