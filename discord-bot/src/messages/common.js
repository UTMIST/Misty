/** Escape Discord markdown and neutralize mentions in untrusted inline text. */
export function escapeInline(value) {
  return String(value)
    .replaceAll('\r', '\\r')
    .replaceAll('\n', '\\n')
    .replace(/([\\`*_{}[\]()<>#|~])/g, '\\$1')
    .replaceAll('@', '@\u200b');
}

export const FALLBACK = 'Something went wrong. Please try again.';
export const DIRECTORY_DOWN_MSG =
  'The directory is temporarily unavailable. Please try again shortly.';
