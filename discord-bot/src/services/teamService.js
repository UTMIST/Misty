import {
  TeamExists,
  TeamNotFound,
  MembershipInvalid,
  DirectoryUnavailable,
  GoogleGroupsNotConfigured,
  GoogleGroupSyncConflict,
} from '../clients/directoryClient.js';
import { resolvePersonTarget, PersonResolutionError } from './personResolver.js';

export const llmSafe = true;

export class MembershipStateChanged extends Error {}

const isoDateToday = () => new Date().toISOString().slice(0, 10);

export function createTeamService({
  directory,
  now = isoDateToday,
  googleProbeTimeoutMs = 2000,
} = {}) {
  async function createTeam({ slug, label, description }, _opts) {
    try {
      const team = await directory.createTeam({ slug, label, description });
      // Directory create already schedules a background sync. Calling sync here
      // is the only way to tell "Google Groups off" (503) from "still
      // provisioning" (GET 404) without a new API, and returns the real mapping.
      // Bound the wait: this retry can queue behind the background sync.
      return { outcome: 'CREATED', team, google: await probeGoogleGroup(team.id) };
    } catch (e) {
      if (e instanceof TeamExists) return { outcome: 'SLUG_EXISTS', detail: e.detail };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function probeGoogleGroup(teamId) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), googleProbeTimeoutMs);
    try {
      const state = await directory.syncTeamGoogleGroup(teamId, { signal: controller.signal });
      return { outcome: 'SYNC_RESULT', state };
    } catch (e) {
      // httpClient maps abort to DirectoryUnavailable; prefer PENDING while our signal is aborted.
      if (controller.signal.aborted) return { outcome: 'PENDING' };
      if (e instanceof GoogleGroupsNotConfigured) return { outcome: 'NOT_CONFIGURED' };
      if (e instanceof GoogleGroupSyncConflict) {
        return { outcome: 'RETIRED_NO_GROUP', detail: e.detail };
      }
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    } finally {
      clearTimeout(timer);
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

  async function addMember(
    { discordSnowflake, personId, reference, teamSlug, teamId, roleKindId, isTeamAdmin },
    _opts,
  ) {
    try {
      const person = await resolvePersonTarget(directory, {
        personId,
        discordSnowflake,
        reference,
      });
      const team = teamId
        ? await directory.getTeam(teamId)
        : await directory.getTeamBySlug(teamSlug);
      if (!team || team.active === false) return { outcome: 'TEAM_NOT_FOUND' };
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
      if (e instanceof PersonResolutionError) return { outcome: e.outcome };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function removeMember({ discordSnowflake, personId, reference, teamSlug, teamId }, _opts) {
    try {
      const person = await resolvePersonTarget(directory, {
        personId,
        discordSnowflake,
        reference,
      });
      const team = teamId
        ? await directory.getTeam(teamId)
        : await directory.getTeamBySlug(teamSlug);
      if (!team || team.active === false) return { outcome: 'TEAM_NOT_FOUND' };
      const active = await directory.listMemberships({
        teamId: team.id,
        personId: person.id,
        activeOnly: true,
      });
      if (active.length === 0) return { outcome: 'NOT_ON_TEAM', person, team };
      await directory.endMembership(active[0].id, now());
      return { outcome: 'REMOVED', person, team };
    } catch (e) {
      if (e instanceof PersonResolutionError) return { outcome: e.outcome };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function prepareMemberUpdate({ personId, discordSnowflake, reference, teamId, teamSlug }) {
    const person = await resolvePersonTarget(directory, { personId, discordSnowflake, reference });
    const team = teamId ? await directory.getTeam(teamId) : await directory.getTeamBySlug(teamSlug);
    if (!team || team.active === false)
      throw new MembershipStateChanged(
        'The team is missing or inactive. Ask again for a fresh preview.',
      );
    const memberships = await directory.listMemberships({
      personId: person.id,
      teamId: team.id,
      activeOnly: true,
      asOf: now(),
    });
    if (
      memberships.length !== 1 ||
      memberships[0].started_at > now() ||
      (memberships[0].ended_at !== null && memberships[0].ended_at <= now())
    ) {
      throw new MembershipStateChanged(
        'There must be exactly one current membership. Check the roster and ask again.',
      );
    }
    return { person, team, membership: Object.freeze({ ...memberships[0] }) };
  }

  async function checkMemberUpdate(expected) {
    const { membership } = await prepareMemberUpdate({
      personId: expected.person_id,
      teamId: expected.team_id,
    });
    if (
      [
        'id',
        'person_id',
        'team_id',
        'started_at',
        'ended_at',
        'role_kind_id',
        'is_team_admin',
        'updated_at',
      ].some((key) => membership[key] !== expected[key])
    ) {
      throw new MembershipStateChanged('The membership changed. Ask again for a fresh preview.');
    }
    return membership;
  }

  async function updateMember({
    personId,
    discordSnowflake,
    teamId,
    teamSlug,
    roleKindId,
    isTeamAdmin,
    expectedMembership,
    beforeWrite,
  }) {
    if (roleKindId === undefined && isTeamAdmin === undefined)
      return { outcome: 'UPDATE_REQUIRED' };
    if (
      (roleKindId !== undefined &&
        !['executive', 'director', 'lead', 'member'].includes(roleKindId)) ||
      (isTeamAdmin !== undefined && typeof isTeamAdmin !== 'boolean')
    )
      return { outcome: 'MEMBERSHIP_INVALID' };
    let writeStarted = false;
    try {
      const current = expectedMembership
        ? await checkMemberUpdate(expectedMembership)
        : (await prepareMemberUpdate({ personId, discordSnowflake, teamId, teamSlug })).membership;
      const changes = {};
      if (roleKindId !== undefined && roleKindId !== current.role_kind_id)
        changes.roleKindId = roleKindId;
      if (isTeamAdmin !== undefined && isTeamAdmin !== current.is_team_admin)
        changes.isTeamAdmin = isTeamAdmin;
      if (!Object.keys(changes).length) return { outcome: 'UNCHANGED', membership: current };
      await beforeWrite?.();
      writeStarted = true;
      const membership = await directory.updateMembership(current.id, changes);
      return membership ? { outcome: 'UPDATED', membership } : { outcome: 'MEMBERSHIP_CHANGED' };
    } catch (error) {
      if (error instanceof DirectoryUnavailable)
        return { outcome: writeStarted ? 'UPDATE_UNCERTAIN' : 'DIRECTORY_DOWN' };
      if (error instanceof MembershipInvalid) return { outcome: 'MEMBERSHIP_INVALID' };
      if (error instanceof MembershipStateChanged || error instanceof PersonResolutionError)
        return { outcome: 'MEMBERSHIP_CHANGED' };
      throw error;
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

  async function getGoogleGroupStatus({ teamSlug }, _opts) {
    try {
      const team = await directory.getTeamBySlug(teamSlug);
      if (!team) return { outcome: 'TEAM_NOT_FOUND' };
      const state = await directory.getTeamGoogleGroup(team.id);
      if (!state) return { outcome: 'NO_MAPPING', team };
      return { outcome: 'STATUS', team, state };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  async function syncGoogleGroup({ teamSlug }, _opts) {
    try {
      const team = await directory.getTeamBySlug(teamSlug);
      if (!team) return { outcome: 'TEAM_NOT_FOUND' };
      const state = await directory.syncTeamGoogleGroup(team.id);
      return { outcome: 'SYNC_RESULT', team, state };
    } catch (e) {
      if (e instanceof TeamNotFound) return { outcome: 'TEAM_NOT_FOUND' };
      if (e instanceof GoogleGroupsNotConfigured) return { outcome: 'NOT_CONFIGURED' };
      if (e instanceof GoogleGroupSyncConflict) {
        return { outcome: 'RETIRED_NO_GROUP', detail: e.detail };
      }
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      throw e;
    }
  }

  return {
    createTeam,
    listTeams,
    renameTeam,
    addMember,
    prepareMemberUpdate,
    checkMemberUpdate,
    updateMember,
    removeMember,
    getRoster,
    getMyTeams,
    getGoogleGroupStatus,
    syncGoogleGroup,
    _now: now,
  };
}
