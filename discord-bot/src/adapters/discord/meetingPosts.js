import { createHash } from 'node:crypto';
import { AttachmentBuilder, MessageFlags, PermissionFlagsBits } from 'discord.js';
import { sendDiscordMessage } from './messages.js';
import { meetingFilename } from '../../meeting/meetingFilename.js';
import { meetingLinkMessage } from '../../messages/meeting.js';

/** Post to the recorded destination, recovering a previous notification where possible. */
export function makeMeetingLinkPoster(client) {
  return async (record) => {
    if (
      !/^[0-9a-f-]{36}$/i.test(record.meeting_id ?? '') ||
      !/^\d{1,20}$/.test(record.guild_id ?? '') ||
      !/^\d{1,20}$/.test(record.text_channel_id ?? '') ||
      !/^[A-Za-z0-9_-]{1,200}$/.test(record.google_document_id ?? '') ||
      record.doc_url !== `https://docs.google.com/document/d/${record.google_document_id}/edit` ||
      record.persistence_status !== 'saved' ||
      record.removed_at ||
      record.delivery_status === 'disabled'
    ) {
      throw new Error('invalid saved meeting reference');
    }
    // The authenticated record's exact launch channel is the notice audience.
    // Fetch it again, even after restart. Never fall back from a private thread
    // to its parent, another text channel, or the stopping command's channel.
    const channel = await client.channels.fetch(record.text_channel_id, { force: true });
    if (
      !channel ||
      channel.id !== record.text_channel_id ||
      channel.guildId !== record.guild_id ||
      !channel.isTextBased() ||
      !channel.messages ||
      !client.user?.id
    ) {
      throw new Error('meeting destination unavailable');
    }

    // Discord can return a successful empty history when Read Message History
    // is denied. Verify effective channel permissions before interpreting any
    // fetch (including a missing receipt) as permission to send a replacement.
    const member = await channel.guild.members.fetchMe({ force: true });
    const permissions = channel.permissionsFor(member);
    if (
      !permissions?.has([PermissionFlagsBits.ViewChannel, PermissionFlagsBits.ReadMessageHistory])
    ) {
      throw new Error('meeting history access unavailable');
    }

    let message;
    if (record.discord_message_id) {
      try {
        message = await channel.messages.fetch({
          message: record.discord_message_id,
          force: true,
          cache: false,
        });
      } catch (error) {
        // Only a confirmed deletion permits replacement. Permission/network
        // failures must not turn an edit into a duplicate send.
        if (error.code !== 10008) throw error;
      }
    }
    const marker = `Meeting ID: \`${record.meeting_id}\``;
    if (!message) {
      // Covers send-success/PATCH-failure and ambiguous sends across restarts.
      // Read failure stops delivery: we cannot safely assume no message exists.
      const recent = await channel.messages.fetch({ limit: 100, cache: false });
      message = recent.find(
        (item) => item.author.id === client.user.id && item.content.endsWith(marker),
      );
    }
    if (message) {
      if (message.author.id !== client.user.id || !message.content.endsWith(marker)) {
        throw new Error('meeting notification receipt mismatch');
      }
      await message.edit({
        ...meetingLinkMessage(record, { editing: true }),
        flags: MessageFlags.SuppressEmbeds,
      });
    } else {
      message = await channel.send({
        ...meetingLinkMessage(record),
        flags: MessageFlags.SuppressEmbeds,
        nonce: createHash('sha256').update(record.meeting_id).digest('hex').slice(0, 25),
        enforceNonce: true,
      });
    }
    return message.id;
  };
}

// Tells a meeting's text channel that its recording died on its own. Never
// throws -- it runs inside teardown, which must complete regardless.
export function makeChannelNotifier() {
  return async ({ channel, content }) => {
    try {
      if (channel) await sendDiscordMessage((part) => channel.send(part), { content });
    } catch {
      console.error('Could not deliver meeting notice');
    }
  };
}

// Decode and post the minutes PDF, mentioning the requester. Posting failures
// propagate so the stop path can report that the minutes could not be delivered.
export function makeAttachmentPoster() {
  return async ({ channel, report, requesterId, name, startedAt }) => {
    try {
      const filename = meetingFilename(name, startedAt);
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
      await sendDiscordMessage((part) => channel.send(part), { content, files: [pdfFile] });
    } catch (e) {
      console.error('meeting minutes post failed');
      throw e;
    }
  };
}
