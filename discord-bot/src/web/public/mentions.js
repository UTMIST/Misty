// Client-side ES module. Also imported directly by node --test (works because
// it's plain ESM with no browser-only APIs).

const MENTION_RE = /<@(\d+)>/g;

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[c],
  );
}

export function hydrateMentions(text, peopleMap) {
  // Split on mentions so we can escape non-mention text but emit real HTML for mentions.
  const s = String(text ?? '');
  const parts = [];
  let last = 0;
  for (const m of s.matchAll(MENTION_RE)) {
    parts.push(escapeHtml(s.slice(last, m.index)));
    const id = m[1];
    const name = peopleMap.get(id);
    if (name) {
      parts.push(`<span class="mention">@${escapeHtml(name)}</span>`);
    } else {
      parts.push(`&lt;@${escapeHtml(id)} (unknown)&gt;`);
    }
    last = m.index + m[0].length;
  }
  parts.push(escapeHtml(s.slice(last)));
  return parts.join('');
}
