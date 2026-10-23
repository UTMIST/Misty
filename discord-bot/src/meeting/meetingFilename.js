function meetingTimestamp(startedAt) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Toronto',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(startedAt));
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${values.year}-${values.month}-${values.day}_${values.hour}${values.minute}`;
}

function meetingFilenamePart(name) {
  const withoutControls = Array.from(name ?? '', (char) =>
    char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127 ? ' ' : char,
  ).join('');
  return withoutControls
    .replace(/[<>:"/\\|?*]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** The single filename rule shared by Q&A labels and transcript/minutes PDFs. */
export function meetingFilename(name, startedAt) {
  const timestamp = meetingTimestamp(startedAt ?? Date.now());
  const safeName = meetingFilenamePart(name);
  return `${safeName || 'meeting'}_${timestamp}.pdf`;
}
