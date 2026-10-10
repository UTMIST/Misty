import { meetingTimestamp } from './meetingPosts.js';

// Discord objects stay at the adapter boundary. The lifecycle holds the exact
// created thread, so cleanup cannot pick another meeting's thread by server ID.
export function makeSubtitleAdapter() {
  const starters = new WeakMap();
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
      const thread = await starter.startThread({
        name: Array.from(title).slice(0, 100).join(''),
        autoArchiveDuration: 1440,
        reason: 'Meeting subtitles',
      });
      starters.set(thread, starter);
      return thread;
    },
    send(thread, content) {
      return thread.send({ content, allowedMentions: { parse: [] } });
    },
    async archive(thread) {
      await thread.setArchived(true, 'Meeting ended');
      const starter = starters.get(thread);
      if (starter) {
        try {
          await starter.edit({
            content: 'Live subtitles for this meeting (archived).',
            allowedMentions: { parse: [] },
          });
        } finally {
          starters.delete(thread);
        }
      }
    },
  };
}
