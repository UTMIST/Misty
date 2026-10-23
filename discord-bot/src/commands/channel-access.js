import { defineCommand } from '../defineCommand.js';
import { allTeamsAutocomplete } from './teamAutocomplete.js';
import {
  renderAppendResult,
  renderClearResult,
  renderNotInGuild,
  renderSetResult,
  renderShowResult,
} from '../messages/channelAccess.js';

const TEAM_OPTION = (name, required, description) => ({
  name,
  type: 'string',
  required,
  description,
  autocomplete: allTeamsAutocomplete,
});

const TEAM_OPTIONS = [
  TEAM_OPTION('team', true, 'Team slug'),
  TEAM_OPTION('team2', false, 'Additional team slug'),
  TEAM_OPTION('team3', false, 'Additional team slug'),
  TEAM_OPTION('team4', false, 'Additional team slug'),
  TEAM_OPTION('team5', false, 'Additional team slug'),
];

function collectSlugs(options) {
  return ['team', 'team2', 'team3', 'team4', 'team5']
    .map((key) => options[key])
    .filter((v) => typeof v === 'string' && v.trim());
}

function scopeOrDeny({ discordGuildId, discordChannelId }) {
  if (!discordGuildId || !discordChannelId) return renderNotInGuild();
  return null;
}

export default defineCommand({
  name: 'channel-access',
  description: 'Choose which teams documents Misty may use in this channel',
  auth: 'admin',
  beta: false,
  options: [],
  subcommands: [
    {
      name: 'show',
      description: 'Show the teams configured for this channel (admin)',
      auth: 'admin',
      options: [],
      async handler({ ctx, discordGuildId, discordChannelId, channelUsedParent }) {
        const denied = scopeOrDeny({ discordGuildId, discordChannelId });
        if (denied) return denied;
        const result = await ctx.channelAccessService.show({
          guildId: discordGuildId,
          channelId: discordChannelId,
        });
        return renderShowResult(result, {
          channelId: discordChannelId,
          usedParent: channelUsedParent,
        });
      },
    },
    {
      name: 'set',
      description: 'Replace the teams whose documents may inform answers here (admin)',
      auth: 'admin',
      options: TEAM_OPTIONS,
      async handler({ options, ctx, discordGuildId, discordChannelId, channelUsedParent }) {
        const denied = scopeOrDeny({ discordGuildId, discordChannelId });
        if (denied) return denied;
        const result = await ctx.channelAccessService.set({
          guildId: discordGuildId,
          channelId: discordChannelId,
          slugs: collectSlugs(options),
        });
        return renderSetResult(result, {
          channelId: discordChannelId,
          usedParent: channelUsedParent,
        });
      },
    },
    {
      name: 'append',
      description: 'Add teams to this channel without removing existing ones (admin)',
      auth: 'admin',
      options: TEAM_OPTIONS,
      async handler({ options, ctx, discordGuildId, discordChannelId, channelUsedParent }) {
        const denied = scopeOrDeny({ discordGuildId, discordChannelId });
        if (denied) return denied;
        const result = await ctx.channelAccessService.append({
          guildId: discordGuildId,
          channelId: discordChannelId,
          slugs: collectSlugs(options),
        });
        return renderAppendResult(result, {
          channelId: discordChannelId,
          usedParent: channelUsedParent,
        });
      },
    },
    {
      name: 'clear',
      description: 'Clear team document scope for this channel (admin)',
      auth: 'admin',
      options: [],
      async handler({ ctx, discordGuildId, discordChannelId, channelUsedParent }) {
        const denied = scopeOrDeny({ discordGuildId, discordChannelId });
        if (denied) return denied;
        const result = await ctx.channelAccessService.clear({
          guildId: discordGuildId,
          channelId: discordChannelId,
        });
        return renderClearResult(result, {
          channelId: discordChannelId,
          usedParent: channelUsedParent,
        });
      },
    },
  ],
  async handler(intent) {
    const sub = this.subcommands.find((s) => s.name === intent.subcommand);
    if (!sub) return { content: 'Something went wrong. Please try again.', ephemeral: true };
    return sub.handler(intent);
  },
});
