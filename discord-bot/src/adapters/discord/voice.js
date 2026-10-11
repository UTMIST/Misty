export const AUTO_STOP_GRACE_MS = 20_000;

// Count the human (non-bot) occupants of a recorded voice channel.
//
// We count from `guild.voiceStates.cache`, NOT `channel.members`. `channel.members`
// resolves each voice state to a `GuildMember` via `guild.members.cache`, which is
// only kept populated by the privileged `GuildMembers` intent — which the bot does
// not request (see src/index.js). Without it that member resolution is unreliable, so a
// still-present member (including the bot itself, whose member often isn't cached)
// can be miscounted, which is why auto-stop wasn't firing. `voiceStates.cache` is
// maintained by `GuildVoiceStates` (which we DO have — it's what powers voice
// receive) and carries each occupant's user id + channel id directly, with no
// member-cache dependency. We exclude the recorder bot by its own user id (the
// reliable signal) and other bots best-effort via any resolved member.
export function humansIn(voiceChannel, botId) {
  // No cache to count from (e.g. the recorded channel is gone/inaccessible, or
  // a caller passed a channel stub without one) => treat as empty rather than
  // throwing. Callers that gate a destructive action on "still occupied" get
  // the fail-open behavior they want here: unknown reads as unoccupied, never
  // as "someone's still in there, leave it running".
  const cache = voiceChannel?.guild?.voiceStates?.cache;
  if (!cache) return 0;
  let count = 0;
  for (const state of cache.values()) {
    if (state.channelId !== voiceChannel.id) continue;
    if (botId && state.id === botId) continue; // the recorder bot itself
    if (state.member?.user?.bot) continue; // other bots (best-effort; may be uncached)
    count += 1;
  }
  return count;
}

const NEW_MEETING_PROMPT =
  'A new meeting is starting. Run `/record start` to begin recording the voice channel.';

// Prompt the first human to enter an otherwise empty voice channel. Starting
// remains an explicit slash command because it is linked-only and consumes a
// voice connection plus a live meeting session.
export function createMeetingPrompt({
  meetingSurface,
  getBotId = () => undefined,
  getUser = () => undefined,
} = {}) {
  const sendPrompt = (user, guildId) => {
    if (!user?.send) return;
    try {
      Promise.resolve(user.send({ content: NEW_MEETING_PROMPT })).catch((err) =>
        console.error(`meeting prompt failed for guild ${guildId}:`, err),
      );
    } catch (err) {
      console.error(`meeting prompt failed for guild ${guildId}:`, err);
    }
  };

  return function onVoiceStateUpdate(oldState, newState) {
    const guild = newState?.guild;
    const guildId = guild?.id;
    const channelId = newState?.channelId;
    if (!guildId || !channelId || oldState?.channelId === channelId) return;

    const botId = getBotId();
    const userId = newState.id ?? newState.member?.id;
    if (!userId || userId === botId || newState.member?.user?.bot) return;
    // The no-meeting-service fallback has no activeSession method. Do not
    // advertise a recording flow that cannot start in that configuration.
    if (typeof meetingSurface?.activeSession !== 'function') return;
    if (meetingSurface.activeSession(guildId)) return;

    const voiceChannel = newState.channel ?? guild.channels?.cache?.get?.(channelId);
    if (humansIn(voiceChannel, botId) !== 1) return;

    const cachedUser = newState.member?.user;
    if (cachedUser?.send) {
      sendPrompt(cachedUser, guildId);
      return;
    }

    try {
      Promise.resolve(getUser(userId))
        .then((user) => sendPrompt(user, guildId))
        .catch((err) => {
          console.error(`meeting prompt failed for guild ${guildId}:`, err);
        });
    } catch (err) {
      console.error(`meeting prompt failed for guild ${guildId}:`, err);
    }
  };
}

// Auto-stop: end a recording when everyone leaves its voice channel. Rather than
// finalizing on the raw "last member left" event (which a transient client blip
// or a voice-region failover would trigger, irreversibly terminating a live
// meeting), we DEBOUNCE: when the recorded channel goes empty we schedule a stop
// after a grace period, and cancel it if a human is back when any later event
// arrives OR if they're back at fire time (re-check).
//
// Each pending timer is bound to the SPECIFIC recording (its `sessionId`) that
// scheduled it. That matters because a guild can record again immediately: if a
// timer scheduled for session A were keyed only by guild, a manual /record stop
// of A followed by a new recording B could let A's stale timer terminate B early
// (and A's still-pending timer would suppress scheduling B's own). Binding to
// sessionId means B always schedules its own full-grace timer, and a timer from
// an ended session no-ops at fire time. Idempotent with `/record stop`.
//
// Injectable timers keep it unit-testable. Returns the voiceStateUpdate handler.
export function createAutoStop({
  meetingSurface,
  getBotId = () => undefined,
  graceMs = AUTO_STOP_GRACE_MS,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
} = {}) {
  const pending = new Map(); // guildId -> { handle, sessionId }

  const cancel = (guildId) => {
    const entry = pending.get(guildId);
    if (entry) {
      clearTimer(entry.handle);
      pending.delete(guildId);
    }
  };

  return function onVoiceStateUpdate(oldState, newState) {
    const guildId = (oldState?.guild ?? newState?.guild)?.id;
    if (!guildId) return;

    const botId = getBotId();
    const session = meetingSurface?.activeSession?.(guildId);
    // Not recording (or session already torn down): drop any pending stop.
    if (!session) return cancel(guildId);
    // Someone is (still/again) present: cancel a pending stop, nothing to do.
    if (humansIn(session.voiceChannel, botId) > 0) return cancel(guildId);

    const existing = pending.get(guildId);
    if (existing) {
      // Already scheduled for THIS recording -> don't stack a second timer.
      if (existing.sessionId === session.sessionId) return;
      // Stale timer from a previous recording in this guild -> replace it.
      clearTimer(existing.handle);
      pending.delete(guildId);
    }

    const { sessionId } = session;
    const handle = setTimer(() => {
      pending.delete(guildId);
      // Re-check at fire time: only stop if it's STILL the same recording and
      // STILL empty (the meeting may have ended, or a human returned).
      const current = meetingSurface?.activeSession?.(guildId);
      if (
        !current ||
        current.sessionId !== sessionId ||
        humansIn(current.voiceChannel, getBotId()) > 0
      ) {
        return;
      }
      Promise.resolve(meetingSurface.stop(guildId)).catch((err) =>
        console.error(`auto-stop failed for guild ${guildId}:`, err?.message ?? err),
      );
    }, graceMs);
    handle?.unref?.(); // don't keep the process alive on the grace timer alone
    pending.set(guildId, { handle, sessionId });
  };
}
