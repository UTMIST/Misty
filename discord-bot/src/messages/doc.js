import { FALLBACK, DIRECTORY_DOWN_MSG } from './common.js';

const DOC_DOWN_MSG =
  'The documentation service is temporarily unavailable. Please try again shortly.';

function docLine(doc, isAdmin) {
  const title = doc.title || doc.url;
  const src = doc.source_id ? ` · ${doc.source_id}` : '';
  const id = isAdmin ? ` · \`${doc.id}\`` : '';
  return `• **${title}**${src} — <${doc.url}>${id}`;
}

export function renderDocAddResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'ADDED':
      case 'MERGED': {
        const verb =
          result.outcome === 'ADDED' ? '✅ Catalogued' : '✅ Already catalogued (tags merged)';
        const title = result.doc.title || result.doc.url;
        const lines = [`${verb}: **${title}**`, result.doc.url, `id: \`${result.doc.id}\``];
        if (result.warnings && result.warnings.length > 0) {
          lines.push(`⚠️ ${result.warnings.join('; ')}`);
        }
        return lines.join('\n');
      }
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'BAD_REFERENCE':
        return `The doc service rejected that: ${result.detail}`;
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}

// `isAdmin` appends each doc's id to its line — only admins can act on an id,
// via /doc remove. Defaults to false so a caller that omits it under-discloses.
export function renderDocListResult(result, { isAdmin = false } = {}) {
  const content = (() => {
    switch (result.outcome) {
      case 'LISTED':
        if (result.docs.length === 0) return 'There are no docs matching that.';
        return result.docs.map((d) => docLine(d, isAdmin)).join('\n');
      case 'TEAM_NOT_FOUND':
        return "There's no team with that slug.";
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      case 'DIRECTORY_DOWN':
        return DIRECTORY_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  // Public: shared reference. Visibility is locked at defer time in the Discord
  // adapter (see doc.js `list` subcommand); this keeps the neutral payload consistent.
  return { content, ephemeral: false };
}

export function renderDocRemoveResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'REMOVED':
        return `✅ Removed **${result.doc.title || result.doc.id}** from the catalog.`;
      case 'NOT_FOUND':
        return "There's no doc with that id.";
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
  return { content, ephemeral: true };
}
