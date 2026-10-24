import { DIRECTORY_DOWN_MSG } from './common.js';

const DRIVE_NOTE =
  "Drive access stays personal — this only chooses which teams' documents Misty may use when answering in this channel.";

function channelLine(channelId, usedParent) {
  const mention = channelId ? `<#${channelId}>` : 'this channel';
  if (usedParent) {
    return `Scope applies to ${mention} (this thread's parent channel).`;
  }
  return `Channel: ${mention}`;
}

function teamList(teams) {
  if (!teams.length) return '_No teams configured — Misty will not use documents in this channel._';
  return teams.map((t) => `• **${t.label}** (\`${t.slug}\`)`).join('\n');
}

function base(channelId, usedParent, body) {
  return {
    content: [channelLine(channelId, usedParent), '', body, '', DRIVE_NOTE].join('\n'),
    ephemeral: true,
    allowedMentions: { parse: [] },
  };
}

export function renderShowResult(result, { channelId, usedParent } = {}) {
  if (result.outcome === 'DIRECTORY_DOWN') {
    return { content: DIRECTORY_DOWN_MSG, ephemeral: true };
  }
  return base(channelId, usedParent, `**Current teams**\n${teamList(result.teams)}`);
}

function renderWriteErrors(result) {
  if (result.outcome === 'DIRECTORY_DOWN') {
    return { content: DIRECTORY_DOWN_MSG, ephemeral: true };
  }
  if (result.outcome === 'NO_TEAMS') {
    return {
      content:
        'Pick at least one team, or use `/channel-access clear` to remove the configuration.',
      ephemeral: true,
    };
  }
  if (result.outcome === 'UNKNOWN_TEAM') {
    return {
      content: `I couldn't find an active team with slug \`${result.slug}\`. Check \`/team list\` and try again.`,
      ephemeral: true,
    };
  }
  if (result.outcome === 'INVALID_TEAMS') {
    return {
      content:
        "One or more teams couldn't be applied (unknown or retired). The previous configuration was left unchanged.",
      ephemeral: true,
    };
  }
  return null;
}

export function renderSetResult(result, { channelId, usedParent } = {}) {
  const err = renderWriteErrors(result);
  if (err) return err;
  return base(
    channelId,
    usedParent,
    `**Updated.** Misty may use documents from these teams in this channel:\n${teamList(result.teams)}`,
  );
}

export function renderAppendResult(result, { channelId, usedParent } = {}) {
  const err = renderWriteErrors(result);
  if (err) return err;
  return base(
    channelId,
    usedParent,
    `**Appended.** Misty may use documents from these teams in this channel:\n${teamList(result.teams)}`,
  );
}

export function renderClearResult(result, { channelId, usedParent } = {}) {
  if (result.outcome === 'DIRECTORY_DOWN') {
    return { content: DIRECTORY_DOWN_MSG, ephemeral: true };
  }
  return base(
    channelId,
    usedParent,
    '**Cleared.** No teams are configured — Misty will not use documents in this channel.',
  );
}

export function renderNotInGuild() {
  return {
    content: 'Use `/channel-access` in a server channel (not in DMs).',
    ephemeral: true,
  };
}
