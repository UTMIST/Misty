import {
  TeamExists,
  TeamNotFound,
  MembershipInvalid,
  DirectoryUnavailable,
} from './directoryClient.js';

export const llmSafe = true;

const isoDateToday = () => new Date().toISOString().slice(0, 10);

export function createTeamService({ directory, now = isoDateToday } = {}) {
  async function createTeam({ slug, label, description }, _opts) {
    try {
      const team = await directory.createTeam({ slug, label, description });
      return { outcome: 'CREATED', team };
    } catch (e) {
      if (e instanceof TeamExists) return { outcome: 'SLUG_EXISTS', detail: e.detail };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function listTeams({ activeOnly } = {}, _opts) {
    try {
      const teams = await directory.listTeams({ activeOnly });
      return { outcome: 'LISTED', teams };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function renameTeam({ slug, newLabel }, _opts) {
    try {
      const team = await directory.getTeamBySlug(slug);
      if (!team) return { outcome: 'TEAM_NOT_FOUND' };
      const updated = await directory.updateTeam(team.id, { label: newLabel });
      return { outcome: 'RENAMED', team: updated };
    } catch (e) {
      if (e instanceof TeamNotFound) return { outcome: 'TEAM_NOT_FOUND' };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function addMember({ discordSnowflake, teamSlug, roleKindId, isTeamAdmin }, _opts) {
    try {
      const person = await directory.getPersonByDiscordId(discordSnowflake);
      if (!person) return { outcome: 'USER_NOT_LINKED' };
      const team = await directory.getTeamBySlug(teamSlug);
      if (!team) return { outcome: 'TEAM_NOT_FOUND' };
      const existing = await directory.listMemberships({
        teamId: team.id,
        personId: person.id,
        activeOnly: true,
      });
      if (existing.length > 0) return { outcome: 'ALREADY_ON_TEAM', person, team };
      try {
        const membership = await directory.createMembership({
          personId: person.id,
          teamId: team.id,
          roleKindId,
          isTeamAdmin,
        });
        return { outcome: 'ADDED', membership, person, team };
      } catch (e) {
        if (e instanceof MembershipInvalid) {
          // Race with a concurrent add? The pre-check said no active membership,
          // but the API rejected. Only claim ALREADY_ON_TEAM when the detail
          // actually says so; otherwise surface the API detail unchanged.
          if (e.detail && /already/i.test(e.detail)) {
            return { outcome: 'ALREADY_ON_TEAM', person, team };
          }
          return { outcome: 'MEMBERSHIP_INVALID', detail: e.detail, person, team };
        }
        throw e;
      }
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function removeMember({ discordSnowflake, teamSlug }, _opts) {
    try {
      const person = await directory.getPersonByDiscordId(discordSnowflake);
      if (!person) return { outcome: 'USER_NOT_LINKED' };
      const team = await directory.getTeamBySlug(teamSlug);
      if (!team) return { outcome: 'TEAM_NOT_FOUND' };
      const active = await directory.listMemberships({
        teamId: team.id,
        personId: person.id,
        activeOnly: true,
      });
      if (active.length === 0) return { outcome: 'NOT_ON_TEAM', person, team };
      await directory.endMembership(active[0].id, now());
      return { outcome: 'REMOVED', person, team };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function getRoster({ teamSlug, asOf }, _opts) {
    try {
      const team = await directory.getTeamBySlug(teamSlug);
      if (!team) return { outcome: 'TEAM_NOT_FOUND' };
      const memberships = await directory.listMemberships({
        teamId: team.id,
        activeOnly: true,
        asOf: asOf ?? now(),
      });
      const persons = await Promise.all(memberships.map((m) => directory.getPerson(m.person_id)));
      const members = memberships
        .map((m, i) => ({
          person: persons[i],
          role_kind_id: m.role_kind_id,
          is_team_admin: m.is_team_admin,
        }))
        .filter((r) => r.person !== null && r.person !== undefined);
      return { outcome: 'ROSTER', team, members };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function getMyTeams({ personId }, _opts) {
    try {
      const memberships = await directory.listMemberships({
        personId,
        activeOnly: true,
      });
      const teams = await Promise.all(memberships.map((m) => directory.getTeam(m.team_id)));
      const withTeams = memberships
        .map((m, i) => ({
          team: teams[i],
          role_kind_id: m.role_kind_id,
          is_team_admin: m.is_team_admin,
          membership: m,
        }))
        .filter((r) => r.team !== null && r.team !== undefined);
      return { outcome: 'MY_TEAMS', memberships: withTeams };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  return {
    createTeam,
    listTeams,
    renameTeam,
    addMember,
    removeMember,
    getRoster,
    getMyTeams,
    _now: now,
  };
}
