import { defineCommand } from '../defineCommand.js';
import { renderAddEmailResult } from '../messages/email.js';

export default defineCommand({
  name: 'add-email',
  description: 'Verify and add another email to your UTMIST record',
  auth: 'linked', // you must already be linked to add emails
  beta: false,
  options: [
    { name: 'email', type: 'string', required: true, description: 'The email to verify and add' },
  ],
  async handler({ options, ctx, discordUserId }) {
    const result = await ctx.emailService.requestEmailCode({ discordUserId, email: options.email });
    return renderAddEmailResult(result);
  },
});
