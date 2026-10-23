import { MessageFlags } from 'discord.js';
import { authMessages } from '../../messages/auth.js';
import { resolvePrincipal } from '../../auth/principal.js';
import { authorize } from '../../auth/policy.js';
import { DirectoryUnavailable } from '../../clients/directoryClient.js';
import { humansIn } from './voice.js';
import { safeReply } from './interactions.js';

// Recording drives Discord voice I/O, so both slash commands and helper actions
// use this adapter runner instead of the neutral command handler. Helpers also
// pass the shared router's requester policy check before reaching this runner.
// commands/record.js supplies registration metadata and authorization policies.

// A single `<#id>` mention is enough: Discord renders it as the channel's
// live name/link client-side, so there's nothing to keep in sync and no user-
// controlled name to wrap in markdown (a channel named e.g. `a_b*c` would
// otherwise break the surrounding bold). It also avoids repeating "in
// **Guild**" once per mention when a reply names the same channel multiple
// times -- every reply is already scoped to the request's own guild.
function describeVoiceChannel(channel) {
  if (!channel) return 'a voice channel';
  if (channel.id) return `<#${channel.id}>`;
  return channel.name ?? 'the voice channel';
}

function currentVoiceChannel(request) {
  return request.voiceChannel ?? null;
}

function activeRecording(meetingSurface, guildId) {
  return typeof meetingSurface.activeSession === 'function'
    ? meetingSurface.activeSession(guildId)
    : null;
}

function sameVoiceChannel(left, right) {
  if (!left || !right) return false;
  return left === right || (left.id && right.id && left.id === right.id);
}

function noRecordingMessage(request) {
  const voiceChannel = currentVoiceChannel(request);
  if (voiceChannel) {
    return `No recording in progress. Misty will join the voice channel ${describeVoiceChannel(voiceChannel)} when you start recording.`;
  }
  return 'No recording in progress. Misty will join the voice channel where you start recording.';
}

function autoStopMessage(voiceChannel) {
  return `Misty will auto-stop recording once everyone leaves ${describeVoiceChannel(voiceChannel)}.`;
}

function busyRecordingMessage(request, active, botId) {
  const recordingChannel = active?.voiceChannel;
  const recordingLocation = describeVoiceChannel(recordingChannel);
  const callerChannel = currentVoiceChannel(request);

  if (sameVoiceChannel(callerChannel, recordingChannel)) {
    return `Misty is already recording in ${recordingLocation}. No new recording was started. ${autoStopMessage(recordingChannel)}`;
  }

  const callerLocation = callerChannel
    ? `You are currently in ${describeVoiceChannel(callerChannel)}.`
    : 'You are not currently in a voice channel.';

  const cache = recordingChannel?.guild?.voiceStates?.cache;
  const empty = cache ? humansIn(recordingChannel, botId) === 0 : false;

  if (empty) {
    return `Misty is already recording in ${recordingLocation}, but the channel is empty and will auto-stop shortly. ${callerLocation} You may not start another recording while one is already active in this server. Run \`/record stop\` to end it immediately, or wait for auto-stop before starting here.`;
  }

  // "This server" (not "elsewhere"/"Misty"): sessions are keyed by guildId, so
  // the one-recording-at-a-time limit is per guild, not a global Misty-wide cap.
  return `Misty is already recording in ${recordingLocation}. ${callerLocation} You may not start another recording while one is already active in this server. Join ${recordingLocation} to participate. ${autoStopMessage(recordingChannel)}`;
}

function activeStatusMessage(request, result, active, botId) {
  const recordingChannel = active?.voiceChannel;
  const recordingLocation = describeVoiceChannel(recordingChannel);
  const elapsed = Math.round((result.elapsedMs ?? 0) / 1000);
  const callerChannel = currentVoiceChannel(request);

  if (sameVoiceChannel(callerChannel, recordingChannel)) {
    return `🔴 Misty is recording in ${recordingLocation} (${elapsed}s elapsed). You are in ${recordingLocation}. ${autoStopMessage(recordingChannel)}`;
  }

  const callerLocation = callerChannel
    ? `You are in ${describeVoiceChannel(callerChannel)}.`
    : 'You are not in a voice channel.';

  const cache = recordingChannel?.guild?.voiceStates?.cache;
  const empty = cache ? humansIn(recordingChannel, botId) === 0 : false;

  if (empty) {
    return `🔴 Misty is recording in ${recordingLocation} (${elapsed}s elapsed), but the channel is empty. ${callerLocation} Misty will auto-stop shortly, or you can run \`/record stop\` to end it now.`;
  }

  return `🔴 Misty is recording in ${recordingLocation} (${elapsed}s elapsed). ${callerLocation} Join ${recordingLocation} to participate or stop the recording. ${autoStopMessage(recordingChannel)}`;
}

function wrongChannelStopMessage(request, active) {
  const recordingLocation = describeVoiceChannel(active?.voiceChannel);
  const callerChannel = currentVoiceChannel(request);
  const callerLocation = callerChannel
    ? `You are currently in ${describeVoiceChannel(callerChannel)}.`
    : 'You are not currently in a voice channel.';
  return `Misty is recording in ${recordingLocation}. ${callerLocation} You should be in ${recordingLocation} to stop the recording, so I left it running. ${autoStopMessage(active?.voiceChannel)}`;
}

export async function handleRecordInteraction(interaction, appContext, recordCommand, botId) {
  await interaction.deferReply({ flags: MessageFlags.Ephemeral }).catch(() => {});
  const payload = await runRecordAction(
    {
      guildId: interaction.guildId,
      requesterId: interaction.user?.id,
      voiceChannel: interaction.member?.voice?.channel,
      textChannel: interaction.channel,
      subcommand: interaction.options.getSubcommand(false),
      name: interaction.options.getString?.('name') ?? null,
      save: interaction.options.getBoolean?.('save') ?? true,
    },
    appContext,
    recordCommand,
    botId ?? interaction.client?.user?.id,
  );
  return safeReply(interaction, payload);
}

export async function runRecordAction(request, appContext, recordCommand, botId) {
  const subcommand = request.subcommand;
  let payload;
  const reply = (content) => {
    payload = { content, ephemeral: true };
  };

  if (!request.guildId) {
    await reply('/record can only be used in a server channel.');
    return payload;
  }

  const resolvedBotId = botId ?? request.client?.user?.id;

  // `/record` bypasses the neutral dispatch, which is where the Policy
  // Enforcement Point normally lives -- so re-run authenticate -> authorize
  // here. Resolve the SAME policy the router would (per-subcommand auth, else
  // command auth, fail-secure to 'linked') from the command metadata rather
  // than hardcoding it, so the two can't drift if record.js is ever retightened.
  // `start` inherits 'linked' (recording consumes resources); `status`/`stop`
  // are declared 'public' so a directory outage can't strand a live recording.
  const activeSub = recordCommand?.subcommands?.find((s) => s.name === subcommand);
  const rawAuth = activeSub?.auth ?? recordCommand?.auth;
  const policy = (typeof rawAuth === 'function' ? rawAuth(request) : rawAuth) ?? 'linked';

  if (policy !== 'public') {
    let principal;
    try {
      principal = await resolvePrincipal(appContext.directory, request.requesterId);
    } catch (e) {
      if (e instanceof DirectoryUnavailable) {
        await reply(authMessages.unavailable().content);
        return payload;
      }
      console.error('record auth lookup failed:', e.message);
      await reply(authMessages.internalError().content);
      return payload;
    }
    const decision = authorize(policy, principal);
    if (!decision.ok) {
      await reply(authMessages.denied(decision.reason).content);
      return payload;
    }
  }

  if (subcommand === 'start') {
    const voiceChannel = request.voiceChannel;
    if (!voiceChannel) {
      await reply(
        'Join a voice channel first. Misty joins the voice channel you are in when recording starts.',
      );
      return payload;
    }

    const active = activeRecording(appContext.meetingSurface, request.guildId);
    if (active) {
      await reply(busyRecordingMessage(request, active, resolvedBotId));
      return payload;
    }

    let result;
    try {
      result = await appContext.meetingSurface.start({
        guildId: request.guildId,
        voiceChannel,
        textChannel: request.textChannel,
        // Remembered for the whole session so the minutes @-mention whoever
        // started the recording, even when auto-stop ends it.
        requesterId: request.requesterId,
        name: request.name ?? null,
        save: request.save ?? true,
      });
    } catch (e) {
      console.error('meetingSurface.start failed:', e.message);
      await reply("Couldn't start recording — the meeting service may be unavailable.");
      return payload;
    }
    if (result.status === 'already-recording') {
      const activeAfterRace = activeRecording(appContext.meetingSurface, request.guildId);
      if (activeAfterRace)
        await reply(busyRecordingMessage(request, activeAfterRace, resolvedBotId));
      else await reply('Misty is already recording, so no new recording was started.');
    } else if (result.status === 'error') {
      // The join failed (missing Connect permission, full channel, timeout).
      // Without this branch the failure fell through to "🔴 Recording…" --
      // the exact false positive this change exists to remove.
      await reply(
        `Couldn't start recording — Misty couldn't join ${describeVoiceChannel(voiceChannel)}.`,
      );
    } else if (result.status === 'unconfigured') await reply("Meeting recording isn't configured.");
    else if (result.status === 'recording') {
      const recordingChannel =
        activeRecording(appContext.meetingSurface, request.guildId)?.voiceChannel ?? voiceChannel;
      await reply(
        `🔴 Misty is now recording in ${describeVoiceChannel(recordingChannel)}. ${autoStopMessage(recordingChannel)}`,
      );
    } else await reply("Couldn't start recording — the recording state is unavailable.");
    return payload;
  }

  if (subcommand === 'status') {
    const result = appContext.meetingSurface.status(request.guildId);
    if (result.status === 'not-recording') await reply(noRecordingMessage(request));
    else {
      const active = activeRecording(appContext.meetingSurface, request.guildId);
      if (active) await reply(activeStatusMessage(request, result, active, resolvedBotId));
      else {
        await reply(
          `🔴 Misty is recording (${Math.round((result.elapsedMs ?? 0) / 1000)}s elapsed), but its voice channel location is unavailable.`,
        );
      }
    }
    return payload;
  }

  if (subcommand === 'stop') {
    const active = activeRecording(appContext.meetingSurface, request.guildId);
    // The wrong-channel guard exists to stop an accidental drive-by stop, not
    // to trap a runaway recording: if auto-stop missed a `voiceStateUpdate`
    // and the recorded channel is now empty (or private/full/deleted and
    // unreadable), a caller sitting outside it is the ONLY escape hatch short
    // of the 4h backstop. So the guard only applies while the recorded
    // channel still has a human in it -- once it's empty, /record stop works
    // from anywhere, same as the public policy already intends.
    const wrongChannel =
      active && !sameVoiceChannel(currentVoiceChannel(request), active.voiceChannel);
    if (wrongChannel && humansIn(active.voiceChannel, resolvedBotId) > 0) {
      await reply(wrongChannelStopMessage(request, active));
      return payload;
    }

    let result;
    try {
      result = await appContext.meetingSurface.stop(request.guildId);
    } catch (e) {
      console.error('meetingSurface.stop failed:', e.message);
      await reply("Couldn't stop recording — please try again.");
      return payload;
    }
    if (result.status === 'not-recording') {
      // `active` was truthy a moment ago (we just passed the wrong-channel
      // guard using it) -- reaching `not-recording` here means the recording
      // ended between that check and this call (auto-stop's own race, or a
      // concurrent /record stop). "No recording in progress ... when you
      // start recording" reads like nothing ever happened; say what did.
      await reply(
        active ? 'The recording already ended — nothing to stop.' : noRecordingMessage(request),
      );
    } else if (result.status === 'error') {
      await reply(
        `Something went wrong stopping the recording in ${describeVoiceChannel(active?.voiceChannel)}. Please try again.`,
      );
    } else if (result.status === 'stopped') {
      await reply(
        `⏳ Misty stopped recording in ${describeVoiceChannel(active?.voiceChannel)}. Processing — minutes will post shortly.`,
      );
    } else await reply("Couldn't determine whether the recording stopped.");
    return payload;
  }

  await reply('Unknown /record subcommand.');
  return payload;
}
