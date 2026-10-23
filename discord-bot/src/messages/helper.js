/** Append retrieved sources even when the model forgets to include citations. */
export function renderHelperAnswer(content, citations = []) {
  const sources = [];
  const seen = new Set();
  for (const citation of citations) {
    let url;
    try {
      url = new URL(citation.url);
    } catch {
      continue;
    }
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) continue;
    const key = citation.doc_id ?? url.href;
    if (seen.has(key)) continue;
    seen.add(key);
    const title = (String(citation.title ?? '').trim() || 'Document')
      .replace(/[\r\n]/g, ' ')
      .slice(0, 160)
      .replace(/([\\`*_{}[\]()<>#|~])/g, '\\$1')
      .replaceAll('@', '@\u200b');
    const href = url.href
      .replaceAll('(', '%28')
      .replaceAll(')', '%29')
      .replace(/[<>\s]/gu, encodeURIComponent);
    // Angle brackets suppress previews while retaining the titled link.
    sources.push(`• [${title}](<${href}>)`);
  }
  return sources.length ? `${content}\n\nSources:\n${sources.join('\n')}` : content;
}
