/** Deliver only saved references. This service never writes Google or meeting content. */
export function createMeetingDelivery({ records, post }) {
  const inFlight = new Map();
  // Remember a successful Discord send if its acknowledgement fails. The
  // adapter also recovers recent messages after a process restart.
  const unacknowledged = new Map();
  const awaitingRecovery = new Set();
  let polling;

  async function attempt(meetingId) {
    let record;
    let messageId = unacknowledged.get(meetingId);
    try {
      // Always re-read: a queued list or /stop result may predate removal.
      record = await records.get(meetingId);
      if (!record) return { status: 'pending' };
      if (record.meeting_id !== meetingId) throw new Error('meeting reference mismatch');
      if (record.removed_at || record.delivery_status === 'disabled') {
        unacknowledged.delete(meetingId);
        return { status: 'disabled', persistenceStatus: record.persistence_status };
      }
      if (record.persistence_status !== 'saved') {
        return { status: 'pending', persistenceStatus: record.persistence_status };
      }
      if (record.delivery_status === 'posted') {
        unacknowledged.delete(meetingId);
        return { status: 'posted' };
      }
      messageId ??= record.discord_message_id;
      messageId = await post({ ...record, discord_message_id: messageId });
      if (!messageId) throw new Error('missing Discord receipt');
      unacknowledged.set(meetingId, messageId);
      const updated = await records.update(meetingId, 'posted', messageId);
      if (!updated) throw new Error('missing delivery acknowledgement');
      unacknowledged.delete(meetingId);
      return { status: 'posted' };
    } catch {
      console.warn(`meeting delivery failed: meeting_id=${meetingId}`);
      // A delivery failure never changes persistence state. Preserve any
      // message receipt so retry edits it rather than sending another copy.
      if (record?.persistence_status === 'saved') {
        await records.update(meetingId, 'failed', messageId).catch(() => {});
      }
      return { status: 'failed', persistenceStatus: record?.persistence_status };
    }
  }

  function deliver(meetingId) {
    if (inFlight.has(meetingId)) return inFlight.get(meetingId);
    const task = attempt(meetingId)
      .then((result) => {
        const unfinishedSave =
          result.status === 'pending' &&
          ['pending', 'creating', 'doc_created', 'shared'].includes(result.persistenceStatus);
        if (unfinishedSave || result.status === 'failed') {
          awaitingRecovery.add(meetingId);
        } else {
          awaitingRecovery.delete(meetingId);
        }
        return result;
      })
      .finally(() => inFlight.delete(meetingId));
    inFlight.set(meetingId, task);
    return task;
  }

  function poll() {
    if (polling) return polling;
    polling = (async () => {
      // Failed deliveries must remain eligible alongside newly saved records.
      // Include known unfinished saves, which are absent from the saved-only lists.
      const pending = new Set(awaitingRecovery);
      let listed = true;
      for (const status of ['pending', 'failed']) {
        try {
          for (const record of await records.list(status)) pending.add(record.meeting_id);
        } catch {
          listed = false;
          console.warn('meeting delivery poll failed');
        }
      }
      for (const meetingId of pending) await deliver(meetingId);
      return { empty: listed && pending.size === 0 && awaitingRecovery.size === 0 };
    })().finally(() => (polling = null));
    return polling;
  }

  return { deliver, poll, hasPendingWork: () => awaitingRecovery.size > 0 };
}

/** Start recovery only after the Discord client is ready; return a teardown hook. */
export function startMeetingDelivery(
  delivery,
  { setIntervalImpl = setInterval, clearIntervalImpl = clearInterval, now = Date.now } = {},
) {
  const retryInterval = 10 * 60_000;
  const maxIdleInterval = 60 * 60_000;
  let interval = retryInterval;
  let nextPollAt = 0;
  let polling = false;
  let stopped = false;

  async function tick() {
    if (stopped || polling) return;
    if (now() < nextPollAt && !delivery.hasPendingWork()) return;
    polling = true;
    // Measure from the start so request duration does not add another timer tick.
    const startedAt = now();
    try {
      const result = await delivery.poll();
      interval = result.empty ? Math.min(interval * 2, maxIdleInterval) : retryInterval;
    } catch {
      interval = retryInterval;
      console.warn('meeting delivery poll failed');
    } finally {
      nextPollAt = startedAt + interval;
      polling = false;
    }
  }

  void tick();
  // Local ticks do not contact the catalog while an empty-poll backoff is active.
  const timer = setIntervalImpl(tick, retryInterval);
  timer.unref?.();
  return () => {
    stopped = true;
    clearIntervalImpl(timer);
  };
}
