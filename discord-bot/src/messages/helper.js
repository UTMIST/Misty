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
    const href = url.href
      .replaceAll('(', '%28')
      .replaceAll(')', '%29')
      .replaceAll('`', '%60')
      .replace(/[<>\s]/gu, encodeURIComponent);
    // Budget the complete encoded link before splitting it into Discord messages.
    const titleBudget = 2000 - `• [](<${href}>)`.length;
    if (titleBudget < 1) {
      sources.push(href.length + 2 <= 2000 ? `<${href}>` : '• Source link is too long to display.');
      continue;
    }
    const titleParts = Array.from(
      (String(citation.title ?? '').trim() || 'Document').replace(/[\r\n]/g, ' ').slice(0, 160),
      (char) => char.replace(/([\\`*_{}[\]()<>#|~])/g, '\\$1').replaceAll('@', '@\u200b'),
    );
    let title = titleParts.join('');
    if (title.length > titleBudget) {
      title = '';
      for (const part of titleParts) {
        if (title.length + part.length > titleBudget - 1) break;
        title += part;
      }
      title += '…';
    }
    // Angle brackets suppress previews while retaining the titled link.
    sources.push(`• [${title}](<${href}>)`);
  }
  return sources.length ? `${content}\n\nSources:\n${sources.join('\n')}` : content;
}
