import { AttachmentBuilder } from 'discord.js';

// Tells a meeting's text channel that its recording died on its own. Never
// throws -- it runs inside teardown, which must complete regardless.
export function makeChannelNotifier() {
  return async ({ channel, content }) => {
    try {
      await channel?.send({ content });
    } catch (e) {
      console.error('meeting notice failed:', e.message);
    }
  };
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

// Decode and post the minutes PDF, mentioning the requester. Posting failures
// propagate so the stop path can report that the minutes could not be delivered.
export function makeAttachmentPoster() {
  return async ({ channel, report, requesterId, name, startedAt }) => {
    try {
      const timestamp = meetingTimestamp(startedAt ?? Date.now());
      const safeName = meetingFilenamePart(name);
      const filename = `${safeName || 'meeting'}_${timestamp}.pdf`;
      const pdfFile = new AttachmentBuilder(Buffer.from(report.pdf_b64, 'base64'), {
        name: filename,
      });
      // No requesterId (e.g. a recording started before this field existed, or
      // any path that couldn't resolve one) => post unaddressed rather than
      // dropping the minutes.
      const content = requesterId ? `<@${requesterId}> 📄 Meeting minutes` : '📄 Meeting minutes';
      // Deliberately NOT swallowed. The service session is destroyed the moment
      // it responds, so if this send fails the minutes are gone for good --
      // reporting success would tell the user "minutes will post here shortly"
      // for a meeting that no longer exists anywhere. Let it propagate so
      // /record stop reports an error and they know to look.
      await channel.send({ content, files: [pdfFile] });
    } catch (e) {
      console.error('meeting minutes post failed:', e.message);
      throw e;
    }
  };
}
