import { DocUnavailable, DocBadReference } from '../clients/docClient.js';
import { DirectoryUnavailable } from '../clients/directoryClient.js';

export const llmSafe = true;

export function createDocService({ docClient, directory }) {
  // Resolve a team slug to its id via the directory. Returns { teamId } on
  // success, { notFound: true } if the slug is unknown. Throws
  // DirectoryUnavailable (caller maps to DIRECTORY_DOWN). Returns { teamId:
  // undefined } when no slug was given.
  async function resolveTeamId(teamSlug) {
    if (teamSlug === undefined || teamSlug === null) return { teamId: undefined };
    const team = await directory.getTeamBySlug(teamSlug);
    if (!team) return { notFound: true };
    return { teamId: team.id };
  }

  async function addDoc({ url, title, teamSlug, tags, owningPersonId }) {
    try {
      const resolved = await resolveTeamId(teamSlug);
      if (resolved.notFound) return { outcome: 'TEAM_NOT_FOUND' };
      const payload = { url };
      if (title !== undefined && title !== null) payload.title = title;
      if (resolved.teamId !== undefined) payload.owningTeamId = resolved.teamId;
      if (owningPersonId) payload.owningPersonId = owningPersonId;
      if (tags !== undefined && tags !== null && tags.length > 0) payload.tags = tags;
      const result = await docClient.ingestDoc(payload);
      return {
        outcome: result.created ? 'ADDED' : 'MERGED',
        doc: result.doc,
        warnings: result.warnings ?? [],
      };
    } catch (e) {
      if (e instanceof DocBadReference) return { outcome: 'BAD_REFERENCE', detail: e.detail };
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      if (e instanceof DocUnavailable) return { outcome: 'DOC_DOWN' };
      throw e;
    }
  }

  async function listDocs({ teamSlug, tag, source, onBehalfOf } = {}) {
    try {
      const resolved = await resolveTeamId(teamSlug);
      if (resolved.notFound) return { outcome: 'TEAM_NOT_FOUND' };
      const docs = await docClient.listDocs({
        owningTeamId: resolved.teamId,
        tag,
        sourceId: source,
        activeOnly: true,
        onBehalfOf,
      });
      return { outcome: 'LISTED', docs };
    } catch (e) {
      if (e instanceof DirectoryUnavailable) return { outcome: 'DIRECTORY_DOWN' };
      if (e instanceof DocUnavailable) return { outcome: 'DOC_DOWN' };
      throw e;
    }
  }

  async function removeDoc({ id }) {
    try {
      const doc = await docClient.deactivateDoc(id);
      if (!doc) return { outcome: 'NOT_FOUND' };
      return { outcome: 'REMOVED', doc };
    } catch (e) {
      if (e instanceof DocUnavailable) return { outcome: 'DOC_DOWN' };
      throw e;
    }
  }

  return { addDoc, listDocs, removeDoc };
}
