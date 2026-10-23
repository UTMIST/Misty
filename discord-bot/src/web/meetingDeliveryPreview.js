import { createMeetingSurface } from '../meeting/meetingSurface.js';
import { createMeetingDelivery } from '../meeting/meetingDelivery.js';
import { meetingLinkMessage } from '../messages/meeting.js';
import { createAutoStop } from '../adapters/discord/voice.js';

export const meetingDeliveryScenarios = [
  'explicit',
  'automatic',
  'retry',
  'delivery-failed',
  'persistence-pending',
  'persistence-failed',
  'admin-only',
];

/** Exercise the real stop/delivery flow with in-memory collaborators only. */
export async function previewMeetingDelivery(scenario) {
  const messages = [];
  const operations = [];
  const record = {
    meeting_id: '22300000-0000-4000-8000-000000000001',
    guild_id: '111111111111111111',
    text_channel_id: '222222222222222222',
    voice_channel_id: '333333333333333333',
    requester_discord_id: '100000000000000000',
    title: 'Playground team meeting',
    started_at: '2026-10-22T18:00:00Z',
    google_document_id: 'playground-example',
    doc_url: 'https://docs.google.com/document/d/playground-example/edit',
    persistence_status:
      scenario === 'persistence-pending'
        ? 'pending'
        : scenario === 'persistence-failed'
          ? 'failed'
          : 'saved',
    audience_team_ids: scenario === 'admin-only' ? [] : ['team-example'],
    delivery_status: 'pending',
    discord_message_id: null,
  };
  let failDelivery = ['retry', 'delivery-failed'].includes(scenario);
  const delivery = createMeetingDelivery({
    records: {
      get: async () => ({ ...record }),
      list: async (status) =>
        record.persistence_status === 'saved' && record.delivery_status === status ? [record] : [],
      update: async (_id, status, messageId) => {
        record.delivery_status = status;
        record.discord_message_id = messageId;
        return { ...record };
      },
    },
    post: async (saved) => {
      operations.push('Discord delivery attempt');
      if (failDelivery) throw new Error('simulated Discord outage');
      if (!messages.some((message) => message.id === 'notification-1')) {
        messages.push({ id: 'notification-1', ...meetingLinkMessage(saved) });
      }
      return 'notification-1';
    },
  });
  const surface = createMeetingSurface({
    genId: () => record.meeting_id,
    now: () => Date.parse(record.started_at),
    meetingClient: {
      openStream: () => ({ close() {}, endAudio() {} }),
      stop: async () => {
        operations.push('Finalize once (fake transcript, minutes and persistence)');
        return { record: { status: record.persistence_status }, pdf_b64: '' };
      },
    },
    makeRecorder: () => ({ start: async () => {}, stop: async () => {} }),
    poster: async () => messages.push({ content: '[Simulated existing PDF attachment]' }),
    notify: async ({ content }) => messages.push({ content }),
    deliverRecord: delivery.deliver,
  });
  const guild = { id: record.guild_id, voiceStates: { cache: new Map() } };
  await surface.start({
    guildId: guild.id,
    voiceChannel: { id: record.voice_channel_id, guild },
    textChannel: { id: record.text_channel_id },
    requesterId: record.requester_discord_id,
    name: record.title,
  });
  if (scenario === 'automatic') {
    let fire;
    let finished;
    const done = new Promise((resolve) => (finished = resolve));
    const autoStop = createAutoStop({
      meetingSurface: {
        ...surface,
        stop: async (id) => {
          await surface.stop(id);
          finished();
        },
      },
      setTimer: (callback) => (fire = callback),
    });
    autoStop({ guild, channelId: record.voice_channel_id }, { guild, channelId: null });
    fire();
    await done;
  } else {
    await surface.stop(guild.id);
  }
  if (scenario === 'retry') {
    failDelivery = false;
    // Recovery uses the saved reference after the live session has gone.
    await delivery.poll();
    await delivery.deliver(record.meeting_id);
  }
  return {
    scenario,
    simulated: true,
    messages,
    operations,
    persistence_status: record.persistence_status,
    delivery_status: record.delivery_status,
    discord_message_id: record.discord_message_id,
  };
}
