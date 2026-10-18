function timestamp(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const seconds = String(total % 60).padStart(2, '0');
  const minutes = String(Math.floor(total / 60) % 60).padStart(2, '0');
  const hours = Math.floor(total / 3600);
  return hours
    ? `${String(hours).padStart(2, '0')}:${minutes}:${seconds}`
    : `${minutes}:${seconds}`;
}

function clip(text, length) {
  if (text.length <= length) return text;
  // Do not leave half a UTF-16 surrogate pair at the display boundary.
  return text.slice(0, Math.max(0, length - 1)).replace(/[\uD800-\uDBFF]$/, '') + '…';
}

function displayLabel(value, limit = 80) {
  return clip(value.replace(/[\r\n]/g, ' ').replace(/[\\`*_~|<>[\]]/g, '\\$&'), limit);
}

/** Render a bounded private answer, reserving space for meeting, sources and scope. */
export function renderQnaAnswer(response, meetingName = 'Meeting') {
  const meeting = `**Meeting: ${displayLabel(meetingName.trim() || 'Meeting', 300)}**\n\n`;
  const range = `${timestamp(response.window_start_ms)}–${timestamp(response.window_end_ms)}`;
  const sources = response.references.length
    ? `\n\n**Sources:** ${response.references.map((ref) => `${displayLabel(ref.speaker)} — ${timestamp(ref.start_ms)}`).join('; ')}`
    : '';
  const scope = response.shortened
    ? `\n\n**Window shortened:** The 15-minute transcript exceeded the size limit. This answer covers **${range}**.`
    : `\n\n**Transcript used:** ${range}`;
  let answer = response.answer;
  let note = '';
  if (meeting.length + answer.length + sources.length + scope.length > 2000) {
    note = '\n\nAnswer shortened for display.';
    answer = clip(answer, 2000 - meeting.length - sources.length - scope.length - note.length);
  }
  return {
    content: meeting + answer + sources + scope + note,
    ephemeral: true,
    allowedMentions: { parse: [] },
  };
}
