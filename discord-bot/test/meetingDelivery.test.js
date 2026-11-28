import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createMeetingDelivery, startMeetingDelivery } from '../src/meeting/meetingDelivery.js';
import { meetingRecordNotice } from '../src/messages/meeting.js';

function setup(overrides = {}) {
  const record = {
    meeting_id: 'meeting-1',
    persistence_status: 'saved',
    delivery_status: 'pending',
    discord_message_id: null,
    ...overrides,
  };
  const posts = [];
  const updates = [];
  const records = {
    get: async () => ({ ...record }),
    list: async (status) => (record.delivery_status === status ? [{ ...record }] : []),
    update: async (id, status, messageId) => {
      updates.push({ id, status, messageId });
      record.delivery_status = status;
      record.discord_message_id = messageId;
      return { ...record };
    },
  };
  const post = async (saved) => {
    posts.push(saved);
    return 'message-1';
  };
  return { record, records, post, posts, updates };
}

test('concurrent immediate/recovery deliveries post once and retain a durable receipt', async () => {
  const f = setup();
  const delivery = createMeetingDelivery(f);
  await Promise.all([
    delivery.deliver('meeting-1'),
    delivery.deliver('meeting-1'),
    delivery.poll(),
  ]);
  await createMeetingDelivery(f).poll(); // restart: posted state prevents a second send
  assert.equal(f.posts.length, 1);
  assert.equal(f.record.delivery_status, 'posted');
  assert.equal(f.record.discord_message_id, 'message-1');
  assert.equal(f.record.persistence_status, 'saved');
});

test('saved delivery failure is recorded and recovered by polling without finalization', async () => {
  const f = setup();
  let broken = true;
  const delivery = createMeetingDelivery({
    ...f,
    post: async (record) => {
      if (broken) throw new Error('Discord denied');
      return f.post(record);
    },
  });
  assert.deepEqual(await delivery.deliver('meeting-1'), {
    status: 'failed',
    persistenceStatus: 'saved',
  });
  assert.equal(f.record.delivery_status, 'failed');
  assert.equal(f.record.persistence_status, 'saved');
  broken = false;
  await delivery.poll();
  assert.equal(f.record.delivery_status, 'posted');
  assert.equal(f.posts.length, 1);
});

test('successful send with failed PATCH preserves receipt for retry even during catalog outage', async () => {
  const f = setup();
  const update = f.records.update;
  f.records.update = async () => {
    throw new Error('catalog down');
  };
  const delivery = createMeetingDelivery(f);
  assert.equal((await delivery.deliver('meeting-1')).status, 'failed');
  assert.equal(f.record.discord_message_id, null);
  f.records.update = update;
  await delivery.deliver('meeting-1');
  assert.equal(f.posts[1].discord_message_id, 'message-1');
  assert.equal(f.record.delivery_status, 'posted');
});

test('known message ID survives a failed edit and process restart', async () => {
  const f = setup({ discord_message_id: 'existing' });
  const failure = createMeetingDelivery({
    ...f,
    post: async () => {
      throw new Error('missing permission');
    },
  });
  await failure.deliver('meeting-1');
  assert.equal(f.record.discord_message_id, 'existing');
  await createMeetingDelivery(f).poll();
  assert.equal(f.posts[0].discord_message_id, 'existing');
});

test('fresh read prevents delivery of removed/disabled/incomplete records', async () => {
  for (const overrides of [
    { persistence_status: 'pending' },
    { persistence_status: 'create_uncertain' },
    { persistence_status: 'doc_created' },
    { persistence_status: 'shared' },
    { persistence_status: 'failed' },
    { persistence_status: 'inaccessible' },
    { persistence_status: 'removed', removed_at: 'now' },
    { delivery_status: 'disabled' },
  ]) {
    const f = setup(overrides);
    await createMeetingDelivery(f).deliver('meeting-1');
    assert.equal(f.posts.length, 0, JSON.stringify(overrides));
    assert.equal(f.updates.length, 0);
  }
});

test('poll continues after one bad record and attempts each failed record once per pass', async () => {
  const f = setup();
  f.records.list = async () => [{ meeting_id: 'bad' }, { meeting_id: 'meeting-1' }];
  f.records.get = async (id) => ({ ...f.record, meeting_id: id });
  const attempts = [];
  const delivery = createMeetingDelivery({
    ...f,
    post: async (record) => {
      attempts.push(record.meeting_id);
      if (record.meeting_id === 'bad') throw new Error('denied');
      return 'ok';
    },
  });
  await delivery.poll();
  assert.deepEqual(attempts, ['bad', 'meeting-1']);
});

async function schedule(delivery) {
  let clock = 0;
  let tick;
  let cleared = false;
  const timer = { unref() {} };
  const stop = startMeetingDelivery(delivery, {
    now: () => clock,
    setIntervalImpl: (callback, milliseconds) => {
      tick = callback;
      assert.equal(milliseconds, 600_000);
      return timer;
    },
    clearIntervalImpl: (value) => {
      assert.equal(value, timer);
      cleared = true;
    },
  });
  await new Promise(setImmediate);
  return {
    async advance(minutes) {
      clock += minutes * 60_000;
      await tick();
    },
    stop() {
      stop();
      assert.equal(cleared, true);
    },
  };
}

test('empty recovery backs off to hourly without catalog requests on skipped ticks', async () => {
  const f = setup({ delivery_status: 'posted' });
  let lists = 0;
  f.records.list = async () => {
    lists++;
    return [];
  };
  const timer = await schedule(createMeetingDelivery(f));
  assert.equal(lists, 2); // Immediate startup recovery.
  await timer.advance(10);
  assert.equal(lists, 2);
  await timer.advance(10);
  assert.equal(lists, 4); // 20-minute empty interval.
  await timer.advance(30);
  assert.equal(lists, 4);
  await timer.advance(10);
  assert.equal(lists, 6); // 40-minute empty interval.
  await timer.advance(50);
  assert.equal(lists, 6);
  await timer.advance(10);
  assert.equal(lists, 8); // 60-minute cap.
  await timer.advance(60);
  assert.equal(lists, 10);
  timer.stop();
  await timer.advance(60);
  assert.equal(lists, 10);
});

test('an immediate delivery failure interrupts idle backoff at the next ten-minute tick', async () => {
  const f = setup({ delivery_status: 'posted' });
  let broken = true;
  const delivery = createMeetingDelivery({
    ...f,
    post: (record) => {
      if (broken) throw new Error('Discord denied');
      return f.post(record);
    },
  });
  const timer = await schedule(delivery);
  await timer.advance(20);
  await timer.advance(40);
  f.record.delivery_status = 'pending';
  assert.equal((await delivery.deliver('meeting-1')).status, 'failed');
  broken = false;
  await timer.advance(10);
  assert.equal(f.posts.length, 1);
  assert.equal(f.record.delivery_status, 'posted');
  assert.equal(delivery.hasPendingWork(), false);
});

test('a known unfinished save is retried even when saved-record lists are empty', async () => {
  const f = setup({ persistence_status: 'pending' });
  f.records.list = async () => [];
  const delivery = createMeetingDelivery(f);
  await delivery.deliver('meeting-1');
  const timer = await schedule(delivery);
  assert.equal(f.posts.length, 0);
  f.record.persistence_status = 'saved';
  await timer.advance(10);
  assert.equal(f.posts.length, 1);
  assert.equal(delivery.hasPendingWork(), false);
});

test('terminal persistence outcomes do not keep empty recovery awake', async () => {
  for (const persistence_status of ['failed', 'inaccessible', 'removed', 'create_uncertain']) {
    const f = setup({ persistence_status });
    const delivery = createMeetingDelivery(f);
    await delivery.deliver('meeting-1');
    assert.equal(delivery.hasPendingWork(), false, persistence_status);
  }
});

test('a missing record stops fast retries and leaves later discovery to the saved-record scan', async () => {
  const f = setup({ persistence_status: 'pending' });
  f.records.list = async () => [];
  const delivery = createMeetingDelivery(f);
  await delivery.deliver('meeting-1');
  assert.equal(delivery.hasPendingWork(), true);
  f.records.get = async () => null;
  await delivery.poll();
  assert.equal(delivery.hasPendingWork(), false);
  assert.deepEqual(await delivery.poll(), { empty: true });
});

test('a failed listing is not treated as empty and retries after ten minutes', async () => {
  for (const failingStatus of ['pending', 'failed']) {
    const f = setup();
    let lists = 0;
    f.records.list = async (status) => {
      lists++;
      if (status === failingStatus) throw new Error('catalog down');
      return [];
    };
    const timer = await schedule(createMeetingDelivery(f));
    assert.equal(lists, 2);
    await timer.advance(10);
    assert.equal(lists, 4);
  }
});

test('timer ticks do not overlap a slow recovery and teardown prevents later polls', async () => {
  let finish;
  let calls = 0;
  const timer = await schedule({
    hasPendingWork: () => false,
    poll: () => {
      calls++;
      return new Promise((resolve) => (finish = resolve));
    },
  });
  await timer.advance(10);
  assert.equal(calls, 1);
  timer.stop();
  finish({ empty: true });
  await timer.advance(60);
  assert.equal(calls, 1);
});

test('notices distinguish failed persistence, pending handoff, removal and failed delivery', () => {
  for (const status of ['pending', 'creating', 'doc_created', 'shared', 'create_uncertain']) {
    assert.match(meetingRecordNotice({ status }, 'id'), /still processing/);
  }
  assert.match(meetingRecordNotice({ status: 'failed' }, 'id'), /save failed/);
  assert.match(meetingRecordNotice({ status: 'handoff_pending' }, 'id'), /30 minutes/);
  assert.match(meetingRecordNotice({ status: 'removed' }, 'id'), /unavailable/);
  assert.match(meetingRecordNotice({ status: 'saved' }, 'id', { status: 'failed' }), /saved, but/);
  assert.equal(meetingRecordNotice({ status: 'saved' }, 'id', { status: 'posted' }), null);
});
