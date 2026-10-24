/** Deliver only saved references. This service never writes Google or meeting content. */
export function createMeetingDelivery({ records, post }) {
  const inFlight = new Map();
  // Remember a successful Discord send if its acknowledgement fails. The
  // adapter also recovers recent messages after a process restart.
  const unacknowledged = new Map();
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
    const task = attempt(meetingId).finally(() => inFlight.delete(meetingId));
    inFlight.set(meetingId, task);
    return task;
  }

  function poll() {
    if (polling) return polling;
    polling = (async () => {
      // Failed deliveries must remain eligible alongside newly saved records.
      const pending = new Set();
      for (const status of ['pending', 'failed']) {
        try {
          for (const record of await records.list(status)) pending.add(record.meeting_id);
        } catch {
          console.warn('meeting delivery poll failed');
        }
      }
      for (const meetingId of pending) await deliver(meetingId);
    })().finally(() => (polling = null));
    return polling;
  }

  return { deliver, poll };
}

/** Start recovery only after the Discord client is ready; return a teardown hook. */
export function startMeetingDelivery(delivery, { setIntervalImpl = setInterval } = {}) {
  void delivery.poll();
  const timer = setIntervalImpl(() => void delivery.poll(), 10 * 60_000);
  timer.unref?.();
  return () => clearInterval(timer);
}
