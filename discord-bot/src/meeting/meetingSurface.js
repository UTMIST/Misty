import { randomUUID } from 'node:crypto';

// Per-guild live-meeting lifecycle. Pure orchestration: all collaborators
// (transport client, recorder factory, poster) are injected. No discord.js
// import here — voiceChannel/textChannel are passed through opaquely.
export function createMeetingSurface({
  meetingClient,
  makeRecorder,
  poster,
  // Tells the meeting's text channel that a recording died on its own. Without
  // it a dropped connection is invisible: the session is gone, /record status
  // says nothing is recording, and nobody knows the meeting was lost.
  notify = async () => {},
  now = Date.now,
  genId = randomUUID,
}) {
  const sessions = new Map();

  // `requesterId` is the Discord user id of whoever started the recording. It's
  // stored on the session (not read off the stopping interaction) so the
  // minutes always @-mention the person who asked for them -- including when
  // the recording ends via auto-stop, where there's no interaction at all.
  async function start({ guildId, voiceChannel, textChannel, requesterId }) {
    if (sessions.has(guildId)) {
      return { status: 'already-recording' };
    }

    const sessionId = genId();
    // Both are read by teardown, which openStream can invoke before either has
    // been assigned -- hence `let ... = null` rather than a const below.
    let recorder = null;
    let stream = null;
    // A teardown can land BEFORE the session is registered (openStream may fail
    // synchronously). The sessionId guard alone no-ops there, and start() would
    // go on to register a session over an already-dead stream, so track the
    // request itself.
    let tornDown = false;
    let registered = false;

    // Idempotent teardown: if the guild's session has already been removed
    // (e.g. a normal stop() raced us, or this fires twice), this is a no-op.
    //
    // It MUST stop the recorder, not just close the stream. Leaving the voice
    // connection up means the bot sits in the channel holding open one receive
    // stream per speaker, while `sessions.delete` makes /record status report
    // nothing is recording and /record stop answer "no recording in progress"
    // -- so nothing short of a restart can remove it.
    const teardown = async (reason) => {
      if (tornDown) return;
      // Once registered, only the registered owner may tear down. A normal
      // stop() deletes the session and then closes the stream, which fires
      // onClose right back at us -- running on would stop an already-stopped
      // recorder and tell the channel the recording "died" seconds after its
      // minutes posted.
      if (registered && sessions.get(guildId)?.sessionId !== sessionId) return;
      tornDown = true;
      const wasRegistered = registered;
      if (wasRegistered) sessions.delete(guildId);
      try {
        if (recorder) await recorder.stop();
      } catch (err) {
        console.error(`meetingSurface: error stopping recorder during teardown for guild ${guildId}:`, err);
      }
      try {
        if (stream) await stream.close();
      } catch (err) {
        console.error(`meetingSurface: error closing stream during teardown for guild ${guildId}:`, err);
      }
      // Nothing to announce if the recording never started: start() returns
      // 'error' and the command reply already says so.
      if (!wasRegistered) return;
      await notify({
        channel: textChannel,
        content: `⚠️ The recording stopped unexpectedly (${reason}) and no minutes will be posted.`,
      }).catch((err) => console.error(`meetingSurface: teardown notify failed:`, err));
    };

    // An unexpected socket death does NOT mean the transcript is gone. The
    // service HOLDS a disconnected session for its disconnect grace period
    // precisely so it can still be finalized over HTTP, so try stop() -- which
    // POSTs /stop and posts the PDF -- before telling anyone the meeting was
    // lost. Tearing down immediately is what threw away two fully-transcribed
    // production meetings in one morning.
    const salvage = async (reason) => {
      if (tornDown) return;
      // Not registered yet (openStream failed synchronously, auth rejected, a
      // duplicate session id): there is no server-side session to finalize, and
      // teardown is also what flags start() to abort.
      if (!registered) return teardown(reason);
      // A normal stop() closes the socket itself and has already deregistered
      // the session by the time this fires. Nothing to salvage, and re-running
      // would double-post the minutes.
      if (sessions.get(guildId)?.sessionId !== sessionId) return;

      const result = await stop(guildId).catch((err) => {
        console.error(`meetingSurface: salvage finalize failed for guild ${guildId}:`, err);
        return { status: 'error' };
      });
      if (result?.status === 'stopped') return;

      // stop() has already deregistered the session and closed the stream, so
      // teardown()'s owner guard would no-op here -- do the two things it would
      // have done. recorder.stop() is idempotent, and stop() skips it if the
      // finalize threw first, so this is what keeps us out of the voice channel.
      tornDown = true;
      try {
        if (recorder) await recorder.stop();
      } catch (err) {
        console.error(`meetingSurface: error stopping recorder after failed salvage for guild ${guildId}:`, err);
      }
      await notify({
        channel: textChannel,
        content: `⚠️ The recording stopped unexpectedly (${reason}) and the minutes could not be recovered.`,
      }).catch((err) => console.error('meetingSurface: salvage notify failed:', err));
    };

    stream = meetingClient.openStream(sessionId, {
      guildId,
      onError: () => salvage('connection error'),
      // A clean close counts too: an auth rejection, a duplicate session, or a
      // service redeploy all close the socket without an error, and every frame
      // after that is silently discarded by the client.
      onClose: () => salvage('connection closed'),
    });
    // openStream failed synchronously and teardown already ran -- but with no
    // `stream` to close yet, so close it here rather than registering it.
    if (tornDown) {
      await Promise.resolve(stream?.close()).catch(() => {});
      return { status: 'error' };
    }
    recorder = makeRecorder({
      sink: stream,
      // The voice connection is gone for good (a 4014 park, or retries
      // exhausted). Audio already sent is fine -- the service has it -- so
      // FINALIZE rather than tear down: stop() posts minutes for what was
      // captured. Doing nothing meant capture silently stopped and /record stop
      // returned a truncated transcript with no hint anything was missing.
      onVoiceLost: () => {
        if (sessions.get(guildId)?.sessionId !== sessionId) return;
        notify({
          channel: textChannel,
          content: '⚠️ I lost the voice connection. Wrapping up the recording — the minutes will cover everything up to that point.',
        }).catch((err) => console.error('meetingSurface: voice-lost notify failed:', err));
        stop(guildId).catch((err) => {
          console.error(`meetingSurface: finalize after voice loss failed for guild ${guildId}:`, err);
        });
      },
    });

    // Register BEFORE awaiting the join, so a teardown firing mid-join can
    // actually find the session and tear it down (it keys off sessionId).
    sessions.set(guildId, {
      sessionId,
      stream,
      recorder,
      textChannel,
      voiceChannel,
      requesterId,
      startedAt: now(),
    });
    registered = true;

    // AWAIT the join. Returning "recording" before the voice connection is up
    // means a missing Connect permission, a full channel, or a 20s timeout is
    // reported to the user as success, and they only find out at /record stop.
    try {
      await recorder.start(voiceChannel);
    } catch (err) {
      console.error(`meetingSurface: recorder failed to start for guild ${guildId}:`, err);
      if (sessions.get(guildId)?.sessionId === sessionId) sessions.delete(guildId);
      await Promise.resolve(stream.close()).catch(() => {});
      return { status: 'error' };
    }

    // A teardown may have removed us while we were joining.
    if (sessions.get(guildId)?.sessionId !== sessionId) return { status: 'error' };

    return { status: 'recording', sessionId };
  }

  function status(guildId) {
    const session = sessions.get(guildId);
    if (!session) return { status: 'not-recording' };
    return { status: 'recording', elapsedMs: now() - session.startedAt };
  }

  // A snapshot of the guild's active recording, or null: its `sessionId` and
  // the (opaque) `voiceChannel` being recorded. The Discord adapter uses this to
  // auto-stop when the channel empties. Both fields come from the single session
  // source of truth so they can't drift; the channel is treated opaquely so this
  // module stays free of any discord.js dependency. The `sessionId` lets the
  // adapter bind a scheduled auto-stop to a SPECIFIC recording, so a timer from
  // an already-ended session can never terminate a later one in the same guild.
  function activeSession(guildId) {
    const session = sessions.get(guildId);
    if (!session) return null;
    return { sessionId: session.sessionId, voiceChannel: session.voiceChannel };
  }

  async function stop(guildId) {
    const session = sessions.get(guildId);
    if (!session) return { status: 'not-recording' };

    // Remove the session BEFORE any awaits so a concurrent stop() call for
    // the same guild can't double-run the teardown below.
    sessions.delete(guildId);

    const { sessionId, stream, recorder, textChannel, requesterId } = session;
    try {
      // Order matters: finalize server-side (POST /stop) BEFORE closing the
      // WS. The service treats a WS disconnect with no prior /stop as an
      // abrupt-disconnect discard() (deregister + drop buffers, no
      // finalize) -- closing the WS first would race the session into being
      // discarded, so the subsequent /stop 404s and we lose the minutes.
      await recorder.stop();
      // Then tell the service that was the last of the audio. This rides the
      // WS behind every frame already sent, so the service can wait for it
      // before finalizing instead of racing the tail. Without it, POST /stop
      // can finalize while the last frames are still in flight and the
      // transcript ends early. Best-effort: a failure here must not cost us
      // the minutes -- the service falls back to a timeout.
      try {
        stream.endAudio();
      } catch (err) {
        console.error(`meetingSurface: end-of-audio signal failed for guild ${guildId}:`, err);
      }
      const report = await meetingClient.stop(sessionId);
      await poster({ channel: textChannel, report, requesterId });
      return { status: 'stopped' };
    } catch (err) {
      console.error(`meetingSurface: error stopping meeting for guild ${guildId}:`, err);
      return { status: 'error' };
    } finally {
      // Close regardless of outcome so a failed recorder.stop()/finalize
      // never leaves the WS connected to the meeting service.
      await Promise.resolve(stream.close()).catch((closeErr) => {
        console.error(`meetingSurface: error closing stream during stop for guild ${guildId}:`, closeErr);
      });
    }
  }

  return { start, status, stop, activeSession };
}
