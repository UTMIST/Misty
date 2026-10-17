import { defineCommand } from '../defineCommand.js';

// Registration metadata, like /record: the Discord adapter supplies the live
// guild/channel context. The neutral playground has no active voice meeting.
export default defineCommand({
  name: 'qna',
  description: 'In progress: Ask about the last 15 minutes of the current recorded meeting',
  auth: 'public',
  beta: false, // In-progress status is informational; register globally.
  ephemeral: true,
  options: [
    {
      name: 'question',
      type: 'string',
      required: true,
      maxLength: 1000,
      description: 'What would you like to know about the current meeting?',
    },
  ],
  async handler() {
    return {
      content: 'Use /qna in the server text channel where the meeting’s recording was started.',
      ephemeral: true,
    };
  },
});
