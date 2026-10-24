import {
  joinVoiceChannel,
  EndBehaviorType,
  entersState,
  VoiceConnectionStatus,
} from '@discordjs/voice';

// After /record stop we keep forwarding until the receive streams have been
// quiet this long, so packets Discord had already buffered still make it out.
const DRAIN_QUIET_MS = 250;
// ...but never longer than this: someone may simply still be talking, and
// /record stop must not wait on them indefinitely.
const DRAIN_MAX_MS = 2000;
const DRAIN_POLL_MS = 50;
// How long to wait for a dropped voice connection to show signs of coming back
// before treating it as gone for good.
const RECONNECT_PROBE_MS = 5000;

export function createRecorder({
  sink,
  // Every time the recorder measures is an ELAPSED duration -- frame
  // timestamps, the drain deadline -- so all of them read this clock, and it
  // must be MONOTONIC. Date.now() goes backwards on an NTP correction, which
  // makes ts_ms negative, and writeBigUInt64BE then throws a RangeError inside
  // the opus 'data' handler, where nothing catches it and the bot has no
  // uncaughtException handler. A backwards jump would also corrupt anchoring on
  // the service side. (There is deliberately no wall-clock `now` here: the
  // recorder has nothing to date, only intervals to measure.)
  monotonic = () => performance.now(),
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  join = joinVoiceChannel,
  // Called when the voice connection is gone beyond recovery, so the surface
  // can finalize what it has instead of leaving the user waiting.
  onVoiceLost = () => {},
  ready = (connection) => entersState(connection, VoiceConnectionStatus.Ready, 20_000),
  // Resolves if the connection is on its way back, rejects if it is gone. The
  // canonical @discordjs/voice reconnect probe.
  reconnecting = (connection) =>
    Promise.race([
      entersState(connection, VoiceConnectionStatus.Signalling, RECONNECT_PROBE_MS),
      entersState(connection, VoiceConnectionStatus.Connecting, RECONNECT_PROBE_MS),
    ]),
}) {
  let connection = null;
  let startedAt = null;
  const knownSpeakers = new Set();
  // Speakers refused after a fetch revealed a bot. Without this, dropSpeaker
  // only clears the subscription: the next speaking event re-enters subscribe(),
  // skips the control frame and the fetch (knownSpeakers already has the id),
  // and re-subscribes -- so the music bot we just dropped keeps being
  // transcribed and billed.
  const refusedSpeakers = new Set();
  // userId -> the speaker's live receive stream, held open for the whole
  // meeting and torn down in stop().
  const subscriptions = new Map();
  let stopped = false;
  let lastPacketAt = 0;

  // Resolve a cached member, if there is one.
  //
  // NOTE: `voiceStates.cache.get(id)?.member` is NOT an independent source --
  // discord.js's VoiceState.member is a getter over `guild.members.cache`
  // (structures/VoiceState.js), and both caches are filled from the same
  // payload in the same handler (actions/VoiceStateUpdate.js). So this is one
  // lookup with a fallback spelling, kept only because the voice-state route
  // reads naturally here.
  //
  // What actually fixes raw snowflakes in the transcript is the members.fetch
  // in subscribe(): the real gap is people already in the channel at
  // GUILD_CREATE, whose voice_states entries carry no member object at all, so
  // nothing is cached for them until we ask the API.
  function resolveMember(guild, userId) {
    return (
      guild?.voiceStates?.cache?.get(userId)?.member ?? guild?.members?.cache?.get(userId) ?? null
    );
  }

  function nameFor(member, userId) {
    return member?.displayName ?? member?.user?.username ?? userId;
  }

  function subscribe(userId, member, guild) {
    if (!knownSpeakers.has(userId)) {
      knownSpeakers.add(userId);
      sink.sendControl({ speakerId: userId, displayName: nameFor(member, userId) });

      // Still unidentified: ask the API. The service takes the display name off
      // the LATEST control frame for a speaker, so a late answer still fixes
      // the name everywhere in the finished transcript.
      if (!member && guild?.members?.fetch) {
        Promise.resolve(guild.members.fetch(userId))
          .then((fetched) => {
            if (!fetched) return;
            if (fetched.user?.bot) {
              // It was a bot after all -- stop transcribing (and paying for) it.
              dropSpeaker(userId);
              return;
            }
            sink.sendControl({ speakerId: userId, displayName: nameFor(fetched, userId) });
          })
          .catch((e) => console.error(`recorder: could not resolve member ${userId}:`, e.message));
      }
    }
    // `receiver.subscribe` returns the same open AudioReceiveStream if one is
    // already active for this userId. Skip re-subscribing so we don't attach
    // duplicate `data`/`error` listeners (which would forward each frame
    // multiple times into the transcription).
    if (subscriptions.has(userId)) return;

    // Manual, NOT AfterSilence. AfterSilence ends the stream a second after
    // someone stops talking, and the receiver silently DISCARDS packets for a
    // user with no subscription (Receiver.onUdpMessage: `if (!stream) return`).
    // Re-subscribing depends on SpeakingMap emitting 'start' again, and its
    // ~100ms speaking timeout is nothing like the 1s stream timeout -- so a
    // speaker who paused could lose everything they said afterwards. Holding
    // one stream open per speaker removes the race entirely: there is always a
    // subscription, so no packet is ever dropped.
    const opus = connection.receiver.subscribe(userId, {
      end: { behavior: EndBehaviorType.Manual },
    });
    subscriptions.set(userId, opus);
    const clearActive = () => subscriptions.delete(userId);
    opus.on('data', (packet) => {
      if (stopped) return;
      lastPacketAt = monotonic();
      // Clamp defensively: a non-monotonic injected clock must degrade to a
      // duplicate timestamp, never to a frame that throws on encode.
      sink.sendFrame(userId, Math.max(0, Math.round(lastPacketAt - startedAt)), packet);
    });
    opus.on('error', (e) => console.error(`recorder: opus stream error for ${userId}:`, e.message));
    // A Manual stream should never end on its own; these only fire if the
    // connection goes away underneath us, and keep the map honest if it does.
    opus.once('error', clearActive);
    opus.once('close', clearActive);
  }

  // Stop capturing a speaker and release their stream (used when a late member
  // fetch reveals a bot).
  function dropSpeaker(userId) {
    refusedSpeakers.add(userId);
    const opus = subscriptions.get(userId);
    subscriptions.delete(userId);
    try {
      opus?.destroy();
    } catch (err) {
      console.error(`recorder: failed to drop speaker ${userId}:`, err.message);
    }
  }

  return {
    async start(voiceChannel) {
      connection = join({
        channelId: voiceChannel.id,
        guildId: voiceChannel.guild.id,
        adapterCreator: voiceChannel.guild.voiceAdapterCreator,
        selfDeaf: false,
        selfMute: true,
      });
      try {
        await ready(connection);
      } catch (e) {
        connection.destroy();
        connection = null;
        throw e;
      }
      startedAt = monotonic();
      const guild = voiceChannel.guild;
      // Never record ourselves. This is the one identity we can always resolve,
      // so it must not depend on the member cache.
      const selfId = voiceChannel.client?.user?.id ?? guild?.members?.me?.id;

      // A voice connection can drop mid-meeting. On close code 4014 the library
      // parks in Disconnected forever and waits for the application to act; on
      // other codes it retries with an unbounded attempt count the application
      // is expected to cap. Neither was handled, so capture simply stopped and
      // /record stop returned a truncated transcript with no hint anything was
      // missing.
      connection.on?.(VoiceConnectionStatus.Disconnected, () => {
        Promise.resolve(reconnecting(connection))
          .then(() => {
            console.error('recorder: voice connection dropped, reconnecting');
          })
          .catch(() => {
            console.error('recorder: voice connection lost for good; capture has stopped');
            onVoiceLost();
          });
      });

      connection.receiver.speaking.on('start', (userId) => {
        if (selfId && userId === selfId) return;
        if (refusedSpeakers.has(userId)) return;
        const member = resolveMember(guild, userId);
        // A KNOWN bot is skipped outright. An unknown user is subscribed
        // optimistically -- refusing would silently drop real people, since the
        // member cache is usually empty -- and dropped later if the fetch above
        // reveals a bot.
        if (member?.user?.bot) return;
        subscribe(userId, member, guild);
      });
    },
    async stop() {
      if (!connection) return; // already stopped
      const conn = connection;
      connection = null; // claim it first, so a concurrent stop() is a no-op

      // Stop taking on NEW speakers, but keep forwarding what is already in
      // flight. destroy() discards whatever Discord has buffered in the receive
      // streams, and setting `stopped` first drops packets that arrive in the
      // meantime -- between them that clipped the last words of the meeting.
      conn.receiver.speaking.removeAllListeners('start');

      const deadline = monotonic() + DRAIN_MAX_MS;
      while (monotonic() < deadline && monotonic() - lastPacketAt < DRAIN_QUIET_MS) {
        await wait(DRAIN_POLL_MS);
      }

      stopped = true;
      // Manual streams never end themselves, so close them explicitly rather
      // than leaving them attached to a destroyed connection.
      for (const opus of subscriptions.values()) {
        try {
          opus.destroy();
        } catch (err) {
          console.error('recorder: failed to close a receive stream:', err.message);
        }
      }
      subscriptions.clear();
      conn.destroy();
    },
  };
}
