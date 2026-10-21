import { ChannelTeamsInvalid, DirectoryUnavailable } from '../clients/directoryClient.js';

export const llmSafe = true;

/**
 * Resolve channel → team access settings through the directory (#238).
 *
 * @param {object} deps
 * @param {object} deps.directory Directory HTTP client.
 */
export function createChannelAccessService({ directory } = {}) {
  async function resolveTeams(slugs) {
    const unique = [...new Set(slugs.map((s) => s.trim().toLowerCase()).filter(Boolean))];
    const teams = [];
    for (const slug of unique) {
      const team = await directory.getTeamBySlug(slug);
      if (!team || team.active === false) return { outcome: 'UNKNOWN_TEAM', slug };
      teams.push(team);
    }
    return { outcome: 'OK', teams };
  }

  async function show({ guildId, channelId }) {
    try {
      const config = await directory.getChannelTeams(guildId, channelId);
      if (!config) return { outcome: 'SHOWN', teams: [], version: null };
      const teams = [];
      for (const id of config.team_ids ?? []) {
        const team = await directory.getTeam(id);
        if (team) teams.push(team);
      }
      return { outcome: 'SHOWN', teams, version: config.version ?? null };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function writeTeams(guildId, channelId, teams, outcome) {
    const config = await directory.replaceChannelTeams(
      guildId,
      channelId,
      teams.map((t) => t.id),
    );
    return { outcome, teams, version: config.version ?? null };
  }

  async function set({ guildId, channelId, slugs }) {
    try {
      const resolved = await resolveTeams(slugs);
      if (resolved.outcome !== 'OK') return resolved;
      if (resolved.teams.length === 0) return { outcome: 'NO_TEAMS' };
      return await writeTeams(guildId, channelId, resolved.teams, 'SET');
    } catch (e) {
      if (e instanceof ChannelTeamsInvalid) {
        return { outcome: 'INVALID_TEAMS', detail: e.detail };
      }
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function append({ guildId, channelId, slugs }) {
    try {
      const resolved = await resolveTeams(slugs);
      if (resolved.outcome !== 'OK') return resolved;
      if (resolved.teams.length === 0) return { outcome: 'NO_TEAMS' };
      const current = await directory.getChannelTeams(guildId, channelId);
      const byId = new Map();
      for (const id of current?.team_ids ?? []) {
        const team = await directory.getTeam(id);
        if (team) byId.set(team.id, team);
      }
      for (const team of resolved.teams) byId.set(team.id, team);
      return await writeTeams(guildId, channelId, [...byId.values()], 'APPENDED');
    } catch (e) {
      if (e instanceof ChannelTeamsInvalid) {
        return { outcome: 'INVALID_TEAMS', detail: e.detail };
      }
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function clear({ guildId, channelId }) {
    try {
      await directory.clearChannelTeams(guildId, channelId);
      return { outcome: 'CLEARED' };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  return { show, set, append, clear };
}
