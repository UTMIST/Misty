const MESSAGE_LIMIT = 2000;
export const DELIVERY_ERROR = 'I couldn’t send the full reply. Please try again shortly.';

/** Split text at newlines, balancing fenced code blocks within each message. */
export function chunkForDiscord(text) {
  const chunks = [];
  let remaining = text ?? '';
  let fence = '';
  while (remaining.length) {
    const prefix = fence ? `${fence}\n` : '';
    // Treat fence markers and their (short) language labels as indivisible.
    const markers = [...remaining.matchAll(/```(?:[^\n`]{0,100}\n)?/g)];
    let cut = Math.min(remaining.length, MESSAGE_LIMIT - prefix.length);
    let nextFence;
    while (true) {
      const crossing = markers.find((m) => m.index < cut && m.index + m[0].length > cut);
      if (crossing) cut = crossing.index;
      if (cut < remaining.length) {
        const newline = remaining.lastIndexOf('\n', cut - 1);
        if (newline > 0) cut = newline + 1;
      }
      // Do not split a UTF-16 surrogate pair (e.g. an emoji).
      if (/[\uD800-\uDBFF]/.test(remaining[cut - 1] ?? '')) cut -= 1;
      nextFence = fence;
      for (const marker of markers) {
        if (marker.index >= cut) break;
        nextFence = nextFence ? '' : marker[0].trimEnd();
      }
      const suffixLength = nextFence ? 4 : 0;
      if (prefix.length + cut + suffixLength <= MESSAGE_LIMIT) break;
      cut -= suffixLength;
    }
    const raw = remaining.slice(0, cut);
    const part = cut < remaining.length || nextFence ? raw.replace(/\n$/, '') : raw;
    chunks.push(prefix + part + (nextFence ? '\n```' : ''));
    remaining = remaining.slice(cut);
    fence = nextFence;
  }
  return chunks;
}

function clip(text, limit) {
  if (text.length <= limit) return text;
  let end = Math.max(0, limit - 1);
  if (/[\uD800-\uDBFF]/.test(text[end - 1] ?? '')) end -= 1;
  return text.slice(0, end) + '…';
}

/** Apply per-property and aggregate embed limits without mutating the renderer. */
export function limitEmbeds(embeds) {
  let budget = 6000;
  let omitted = embeds.length > 10;
  const take = (text, limit) => {
    if (text == null) return undefined;
    if (budget === 0) {
      omitted = true;
      return undefined;
    }
    const value = clip(text, Math.min(limit, budget));
    budget -= value.length;
    return value;
  };
  const limited = embeds
    .slice(0, 10)
    .map((embed) => {
      const source = typeof embed.toJSON === 'function' ? embed.toJSON() : embed;
      const out = { ...source };
      for (const [key, limit] of [
        ['title', 256],
        ['description', 4096],
      ]) {
        const value = take(source[key], limit);
        if (value === undefined) delete out[key];
        else out[key] = value;
      }
      if (source.author) {
        const name = take(source.author.name, 256);
        if (name) out.author = { ...source.author, name };
        else delete out.author;
      }
      if (source.footer) {
        const text = take(source.footer.text, 2048);
        if (text) out.footer = { ...source.footer, text };
        else delete out.footer;
      }
      if (source.fields) {
        out.fields = [];
        for (const field of source.fields.slice(0, 25)) {
          if (budget < 2) break;
          const name = take(field.name, Math.min(256, budget - 1));
          const value = take(field.value, 1024);
          out.fields.push({ ...field, name, value });
        }
        if (out.fields.length < source.fields.length) omitted = true;
      }
      return out;
    })
    .filter(
      (embed) =>
        embed.title ||
        embed.description ||
        embed.author ||
        embed.footer ||
        embed.fields?.length ||
        embed.image ||
        embed.thumbnail,
    );
  if (omitted) {
    // An ellipsis also signals entire properties/fields/embeds that no longer fit.
    const last = limited.at(-1);
    if (last) {
      const target = last.fields?.at(-1) ?? last.footer ?? last.author ?? last;
      const key = target.value
        ? 'value'
        : target.text
          ? 'text'
          : target.name
            ? 'name'
            : target.description
              ? 'description'
              : 'title';
      if (target[key]) target[key] = clip(target[key] + '…', target[key].length);
    }
  }
  return limited;
}

/** Keep embeds/files on the first part and mention controls on every part. */
export function discordMessageParts(payload) {
  if (typeof payload === 'string') return chunkForDiscord(payload);
  const { content, embeds, ...rest } = payload;
  const chunks = chunkForDiscord(content);
  if (!chunks.length) chunks.push(content);
  return chunks.map((chunk, index) => {
    if (index === 0) {
      return {
        ...rest,
        ...(chunk !== undefined && { content: chunk }),
        ...(embeds && { embeds: limitEmbeds(embeds) }),
      };
    }
    return {
      content: chunk,
      ...(rest.allowedMentions && { allowedMentions: rest.allowedMentions }),
    };
  });
}

/** Send channel/DM/reply parts in order; report failure and preserve it for callers. */
export async function sendDiscordMessage(send, payload) {
  try {
    for (const part of discordMessageParts(payload)) await send(part);
  } catch (error) {
    const fallback =
      typeof payload === 'string'
        ? DELIVERY_ERROR
        : { content: DELIVERY_ERROR, allowedMentions: { parse: [] } };
    try {
      await send(fallback);
    } catch {
      /* Delivery may be unavailable altogether. */
    }
    throw error;
  }
}
