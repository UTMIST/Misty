import { meetingTimestamp } from './meetingPosts.js';

// Discord objects stay at the adapter boundary. The lifecycle holds the exact
// created thread, so cleanup cannot pick another meeting's thread by server ID.
export function makeSubtitleAdapter() {
  return {
    async createThread({ channel, name, startedAt }) {
      const parent = channel.isThread?.()
        ? (channel.parent ?? (await channel.guild.channels.fetch(channel.parentId)))
        : channel;
      if (!parent?.send) throw new Error('subtitle parent channel unavailable');
      const starter = await parent.send({
        content: 'Live subtitles for this meeting.',
        allowedMentions: { parse: [] },
      });
      const title = `Subtitles — ${name || meetingTimestamp(startedAt ?? Date.now())}`;
      return starter.startThread({
        name: Array.from(title).slice(0, 100).join(''),
        autoArchiveDuration: 1440,
        reason: 'Meeting subtitles',
      });
    },
    send(thread, content) {
      return thread.send({ content, allowedMentions: { parse: [] } });
    },
    archive(thread) {
      return thread.setArchived(true, 'Meeting ended');
    },
  };
}
