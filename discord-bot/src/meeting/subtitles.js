// One meeting's subtitle state, independent of the active server/session map.
// Socket sequence means delivery order. Speech time only sorts within a batch.
const MAX_BYTES = 256 * 1024;

function escapeText(value) {
  return value.replace(/[\r\n\t]+/g, ' ').replace(/([\\`*_~|>])/g, '\\$1');
}

function timestamp(ms) {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const pad = (n) => String(n).padStart(2, '0');
  return minutes >= 60
    ? `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}:${pad(seconds % 60)}`
    : `${pad(minutes)}:${pad(seconds % 60)}`;
}

function messages(events) {
  const output = [];
  let current = '';
  for (const event of events.sort((a, b) => a.start_ms - b.start_ms || a.sequence - b.sequence)) {
    const line = `[${timestamp(event.start_ms)}] **${escapeText(event.display_name)}:** ${escapeText(event.text)}`;
    if (current && current.length + 1 + line.length <= 2000) {
      current += `\n${line}`;
      continue;
    }
    if (current) output.push(current);
    current = '';
    // Count UTF-16 units for Discord's limit, but iterate whole code points.
    for (const char of line) {
      if (current.length + char.length > 2000) {
        output.push(current);
        current = '';
      }
      current += char;
    }
  }
  if (current) output.push(current);
  return output;
}

export function createSubtitles({
  sessionId,
  channel,
  name,
  startedAt,
  adapter,
  notify = async () => {},
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  // TEMP_SUBTITLE_LATENCY: temporary measurement stubs; remove after branch testing.
  monotonic = () => performance.now(),
  logLatency = (record) => console.info('TEMP_SUBTITLE_LATENCY', JSON.stringify(record)),
}) {
  let active = false;
  let ready = false;
  let closing = false;
  let closed = false;
  let incomplete = false;
  let notified = false;
  let lastSequence = 0;
  let pending = [];
  let bytes = 0;
  let batchTimer;
  let readyTimer;
  let thread;
  let setup;
  let sending = Promise.resolve();
  // TEMP_SUBTITLE_LATENCY: remove this state and measure() calls after branch testing.
  const latencyStartedAt = monotonic();
  let firstChunkReceivedAt;
  let firstChunkSent = false;
  function measure(event, extra = {}) {
    try {
      logLatency({
        session_id: sessionId,
        event,
        elapsed_ms: Math.round(monotonic() - latencyStartedAt),
        ...extra,
      });
    } catch {
      // Temporary diagnostics must never interrupt subtitle delivery.
    }
  }
  let resolveDone;
  const done = new Promise((resolve) => {
    resolveDone = resolve;
  });

  const clear = (timer) => {
    if (timer !== undefined) clearTimer(timer);
  };
  function notice() {
    if (notified) return;
    notified = true;
    Promise.resolve()
      .then(() =>
        notify({
          channel,
          content:
            '⚠️ Live subtitles are unavailable or incomplete. Recording and minutes are handled separately.',
        }),
      )
      .catch(() => {});
  }

  function bounded(operation, ms = 5000) {
    return new Promise((resolve, reject) => {
      const timer = setTimer(() => reject(new Error('subtitle operation timed out')), ms);
      Promise.resolve()
        .then(operation)
        .then(resolve, reject)
        .finally(() => clearTimer(timer));
    });
  }

  function flush(firstOnly = false) {
    clear(batchTimer);
    batchTimer = undefined;
    if (!pending.length || !setup) return;
    const batch = pending.splice(0, firstOnly ? 1 : pending.length);
    const size = batch.reduce((sum, e) => sum + Buffer.byteLength(JSON.stringify(e)), 0);
    sending = sending
      .then(async () => {
        await setup;
        if (!thread || closed) return;
        for (const content of messages(batch)) {
          if (closed) return;
          // TEMP_SUBTITLE_LATENCY: measure the first successful Discord request only.
          const sendStartedAt = monotonic();
          if (!firstChunkSent) measure('first_discord_send_started');
          await bounded(() => adapter.send(thread, content));
          if (!firstChunkSent) {
            firstChunkSent = true;
            measure('first_chunk_sent', {
              received_to_send_ms: Math.round(monotonic() - firstChunkReceivedAt),
              discord_send_ms: Math.round(monotonic() - sendStartedAt),
            });
          }
        }
      })
      .catch(() => {
        fail();
      })
      .finally(() => {
        bytes -= size;
      });
  }

  function close(isIncomplete = false) {
    if (closed) return done;
    incomplete ||= isIncomplete;
    if (isIncomplete) notice();
    if (closing) return done;
    closing = true;
    clear(readyTimer);
    clear(batchTimer);
    flush();
    const drain = sending;
    // Completion is serialized after all received chunks, including stop tails.
    (async () => {
      try {
        if (setup) await setup;
        await drain;
        if (thread) {
          await bounded(() =>
            adapter.send(
              thread,
              incomplete ? 'Subtitles ended — history may be incomplete.' : 'Subtitles ended.',
            ),
          );
        }
      } catch {
        incomplete = true;
        notice();
      } finally {
        if (thread) await bounded(() => adapter.archive(thread)).catch(() => notice());
        closed = true;
        pending = [];
        resolveDone();
      }
    })();
    return done;
  }

  function fail() {
    incomplete = true;
    notice();
    close(true);
  }

  function prepare() {
    if (!active || !ready || setup || closing || closed) return;
    clear(readyTimer);
    setup = bounded(async () => {
      const created = await adapter.createThread({ channel, name, startedAt });
      // A request can finish after our timeout/cancellation. Clean up only its
      // own newly created thread; never the channel the command launched in.
      if (closed) {
        await adapter.archive(created).catch(() => {});
        return;
      }
      thread = created;
      measure('thread_ready'); // TEMP_SUBTITLE_LATENCY
    }).catch(() => {
      fail();
    });
    // Only the first queued chunk bypasses batching, even before readiness.
    if (pending.length) {
      flush(true);
      if (pending.length) batchTimer = setTimer(flush, 2000);
    }
  }

  return {
    activeThreadId() {
      return !closing && !closed ? (thread?.id ?? null) : null;
    },
    activate() {
      if (active || closing || closed) return;
      active = true;
      measure('captions_activated'); // TEMP_SUBTITLE_LATENCY
      if (!ready) readyTimer = setTimer(fail, 5000);
      prepare();
    },
    event(event) {
      if (closing || closed || event.session_id !== sessionId) return;
      if (event.type === 'session.ready') {
        ready = true;
        measure('session_ready'); // TEMP_SUBTITLE_LATENCY
        prepare();
        return;
      }
      if (event.type === 'subtitles.error') {
        fail();
        return;
      }
      if (event.type === 'subtitles.complete') {
        close(event.status !== 'complete' || event.last_sequence !== lastSequence);
        return;
      }
      if (event.type !== 'subtitles.chunk') return;
      if (event.sequence <= lastSequence) return;
      if (event.sequence !== lastSequence + 1) {
        fail();
        return;
      }
      const size = Buffer.byteLength(JSON.stringify(event));
      if (bytes + size > MAX_BYTES) {
        fail();
        return;
      }
      lastSequence = event.sequence;
      bytes += size;
      pending.push(event);
      if (lastSequence === 1) {
        // TEMP_SUBTITLE_LATENCY: remove the timestamp and measurement call after testing.
        firstChunkReceivedAt = monotonic();
        measure('first_chunk_received', { speaker_id: event.speaker_id, sequence: event.sequence });
        if (setup) flush(true);
      } else if (setup && batchTimer === undefined) {
        batchTimer = setTimer(flush, 2000);
      }
    },
    connectionLost() {
      return close(true);
    },
    async finish() {
      // One total deadline, including completion wait and Discord cleanup.
      await bounded(() => done, 30000).catch(() => {
        close(true);
        closed = true;
        clear(batchTimer);
        clear(readyTimer);
        pending = [];
        notice();
      });
    },
    cancel() {
      closed = true;
      clear(batchTimer);
      clear(readyTimer);
      pending = [];
      resolveDone();
    },
  };
}
