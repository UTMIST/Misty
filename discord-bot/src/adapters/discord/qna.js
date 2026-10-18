import { MessageFlags } from 'discord.js';
import { MeetingQnaError } from '../../meeting/meetingClient.js';
import { renderQnaAnswer } from '../../messages/qna.js';

const INVALID_QUESTION = 'Enter a question containing text, up to **1,000 characters**.';

function backendMessage(status) {
  if (status === 404 || status === 409) {
    return 'This recording is ending or has ended. /qna is available while the recording is active.';
  }
  if (status === 413) {
    return 'The latest transcript passage is too large to process. Please try again after more speech has been transcribed.';
  }
  if (status === 422) return INVALID_QUESTION;
  if (status === 429) {
    return 'Meeting Q&A is busy answering other questions. Please try again shortly.';
  }
  if (status === 502) {
    return 'I couldn’t generate a reliable answer from the transcript. Please try again.';
  }
  if (status === 504) return 'The answer took too long to generate. Please try again shortly.';
  return 'Meeting Q&A is temporarily unavailable. Please try again shortly.';
}

/** Dedicated meeting adapter: channel context, no directory/user scoping. */
export async function handleQnaInteraction(interaction, appContext) {
  await interaction.deferReply({ flags: MessageFlags.Ephemeral });
  const reply = (content) => interaction.editReply({ content, allowedMentions: { parse: [] } });
  if (!interaction.guildId) {
    return reply('Use /qna in the server text channel where the meeting’s recording was started.');
  }
  const question = interaction.options.getString('question');
  if (typeof question !== 'string' || !question.trim() || [...question].length > 1000) {
    return reply(INVALID_QUESTION);
  }
  if (typeof appContext.meetingSurface?.qna !== 'function') {
    return reply('Meeting Q&A isn’t configured on this bot.');
  }
  try {
    const result = await appContext.meetingSurface.qna({
      guildId: interaction.guildId,
      channelId: interaction.channelId,
      question: question.trim(),
    });
    if (result.status === 'unconfigured') return reply('Meeting Q&A isn’t configured on this bot.');
    if (result.status === 'not-recording') {
      return reply(
        'No active meeting in this server. Start a recording with /record start to use /qna.',
      );
    }
    if (result.status === 'wrong-channel') {
      return reply(
        result.channelId
          ? `Use /qna in <#${result.channelId}>, where this meeting’s recording was started.`
          : 'Use /qna in the text channel where this meeting’s recording was started.',
      );
    }
    if (result.status !== 'answered') return reply(backendMessage(503));
    const { content, allowedMentions } = renderQnaAnswer(result.response, result.meetingName);
    return await interaction.editReply({ content, allowedMentions });
  } catch (error) {
    // Never log provider errors, questions, or generated/document content.
    return reply(backendMessage(error instanceof MeetingQnaError ? error.status : 503));
  }
}
