import { FALLBACK, DIRECTORY_DOWN_MSG } from './common.js';

const DOC_DOWN_MSG =
  'The documentation service is temporarily unavailable. Please try again shortly.';

function docLine(doc) {
  const title = doc.title || doc.url;
  const src = doc.source_id ? ` · ${doc.source_id}` : '';
  return `• **${title}**${src} — \`${doc.id}\``;
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

export function renderDocListResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'LISTED':
        if (result.docs.length === 0) return 'There are no docs matching that.';
        return result.docs.map(docLine).join('\n');
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

export function renderDocShowResult(result) {
  const content = (() => {
    switch (result.outcome) {
      case 'SHOWN': {
        const d = result.doc;
        const lines = [`**${d.title || d.url}**`, d.url];
        if (d.description) lines.push(d.description);
        if (d.owning_team_label) lines.push(`Owner: ${d.owning_team_label}`);
        if (d.tags && d.tags.length > 0) lines.push(`Tags: ${d.tags.join(', ')}`);
        if (d.source_id) lines.push(`Source: ${d.source_id}`);
        lines.push(`id: \`${d.id}\``);
        return lines.join('\n');
      }
      case 'NOT_FOUND':
        return "There's no doc with that id.";
      case 'DOC_DOWN':
        return DOC_DOWN_MSG;
      default:
        return FALLBACK;
    }
  })();
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
