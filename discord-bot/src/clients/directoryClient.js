import { createHttpClient } from './httpClient.js';

export class DirectoryUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = 'DirectoryUnavailable';
  }
}

export class TeamExists extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'TeamExists';
    this.detail = detail;
  }
}

export class TeamNotFound extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'TeamNotFound';
    this.detail = detail;
  }
}

export class MembershipInvalid extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'MembershipInvalid';
    this.detail = detail;
  }
}

export class AlreadyLinked extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'AlreadyLinked';
    this.detail = detail;
  }
}

export class PersonExists extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'PersonExists';
    this.detail = detail;
  }
}

export class EmailAlreadyRegistered extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'EmailAlreadyRegistered';
    this.detail = detail;
  }
}

export function createDirectoryClient({ baseUrl, apiKey, fetchImpl = fetch }) {
  const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' };
  const { send, parseJson } = createHttpClient({
    baseUrl,
    headers,
    fetchImpl,
    networkError: () => new DirectoryUnavailable('network error reaching directory'),
    parseError: () => new DirectoryUnavailable('malformed directory response'),
  });

  async function getByPath(path) {
    const resp = await send(path);
    if (resp.status === 404) return null;
    if (!resp.ok) throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    return parseJson(resp);
  }

  return {
    async getSelfKeyScopes() {
      const resp = await send('/api-keys/self');
      if (!resp.ok) throw new DirectoryUnavailable(`directory returned ${resp.status}`);
      const body = await parseJson(resp);
      return body.scopes;
    },

    getPersonByEmail(email) {
      return getByPath(`/people/by-email/${encodeURIComponent(email)}`);
    },

    getPersonByDiscordId(snowflake) {
      return getByPath(`/people/by-identifier/discord/${encodeURIComponent(snowflake)}`);
    },

    async listIdentifiers(personId) {
      const resp = await send(`/people/${encodeURIComponent(personId)}/identifiers`);
      if (resp.status === 404) return [];
      if (!resp.ok) throw new DirectoryUnavailable(`directory returned ${resp.status}`);
      return parseJson(resp);
    },

    async linkDiscord(personId, { externalId, handle }) {
      const resp = await send(`/people/${encodeURIComponent(personId)}/identifiers`, {
        method: 'POST',
        body: JSON.stringify({ provider: 'discord', external_id: externalId, handle }),
      });
      if (resp.status === 201) return parseJson(resp);
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        throw new AlreadyLinked(body.detail ?? 'already linked');
      }
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },

    async addEmailIdentifier(personId, email) {
      const resp = await send(`/people/${encodeURIComponent(personId)}/emails`, {
        method: 'POST',
        body: JSON.stringify({ email }),
      });
      if (resp.status === 201) return parseJson(resp);
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        throw new EmailAlreadyRegistered(body.detail ?? 'email already registered');
      }
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },

    async createPerson({ displayName, primaryEmail, accessLevel }) {
      const resp = await send('/people', {
        method: 'POST',
        body: JSON.stringify({
          display_name: displayName,
          primary_email: primaryEmail,
          access_level: accessLevel,
        }),
      });
      if (resp.status === 201) return parseJson(resp);
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        throw new PersonExists(body.detail ?? 'person already exists');
      }
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },

    async listPeople() {
      const resp = await send('/people');
      if (!resp.ok) throw new DirectoryUnavailable(`directory returned ${resp.status}`);
      return parseJson(resp);
    },

    async listTeams({ activeOnly } = {}) {
      const qs = activeOnly ? '?active_only=true' : '';
      const resp = await send(`/teams${qs}`);
      if (!resp.ok) throw new DirectoryUnavailable(`directory returned ${resp.status}`);
      return parseJson(resp);
    },

    getTeamBySlug(slug) {
      return getByPath(`/teams/by-slug/${encodeURIComponent(slug)}`);
    },

    getTeam(teamId) {
      return getByPath(`/teams/${encodeURIComponent(teamId)}`);
    },

    async createTeam({ slug, label, description }) {
      const body = { slug, label };
      if (description !== undefined) body.description = description;
      const resp = await send('/teams', { method: 'POST', body: JSON.stringify(body) });
      if (resp.status === 201) return parseJson(resp);
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        throw new TeamExists(body.detail ?? 'team already exists');
      }
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },

    async updateTeam(teamId, patch) {
      const resp = await send(`/teams/${encodeURIComponent(teamId)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      if (resp.status === 200) return parseJson(resp);
      if (resp.status === 404) {
        const body = await resp.json().catch(() => ({}));
        throw new TeamNotFound(body.detail ?? 'team not found');
      }
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        throw new TeamExists(body.detail ?? 'team already exists');
      }
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },

    getPerson(personId) {
      return getByPath(`/people/${encodeURIComponent(personId)}`);
    },

    async createMembership({ personId, teamId, roleKindId, isTeamAdmin }) {
      const payload = { person_id: personId, team_id: teamId };
      if (roleKindId !== undefined) payload.role_kind_id = roleKindId;
      if (isTeamAdmin !== undefined) payload.is_team_admin = isTeamAdmin;
      const resp = await send('/memberships', { method: 'POST', body: JSON.stringify(payload) });
      if (resp.status === 201) return parseJson(resp);
      if (resp.status === 400) {
        const body = await resp.json().catch(() => ({}));
        throw new MembershipInvalid(body.detail ?? 'membership invalid');
      }
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },

    async listMemberships({ teamId, personId, activeOnly, asOf, isTeamAdmin } = {}) {
      const qs = new URLSearchParams();
      if (teamId) qs.set('team_id', teamId);
      if (personId) qs.set('person_id', personId);
      if (activeOnly) qs.set('active_only', 'true');
      if (asOf) qs.set('as_of', asOf);
      if (isTeamAdmin !== undefined) qs.set('is_team_admin', String(isTeamAdmin));
      const qstr = qs.toString();
      const resp = await send(qstr ? `/memberships?${qstr}` : '/memberships');
      if (!resp.ok) throw new DirectoryUnavailable(`directory returned ${resp.status}`);
      return parseJson(resp);
    },

    async endMembership(membershipId, endedAt) {
      const resp = await send(`/memberships/${encodeURIComponent(membershipId)}/end`, {
        method: 'POST',
        body: JSON.stringify({ ended_at: endedAt }),
      });
      if (resp.status === 200) return parseJson(resp);
      if (resp.status === 404) return null;
      throw new DirectoryUnavailable(`directory returned ${resp.status}`);
    },
  };
}
