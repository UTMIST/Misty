import { defineCommand } from '../defineCommand.js';
import { renderVerifyEmailResult } from '../messages.js';

export default defineCommand({
  name: 'verify-email',
  description: 'Confirm the code emailed to you to finish adding your email',
  auth: 'linked',
  beta: false,
  options: [
    {
      name: 'code',
      type: 'string',
      required: true,
      description: 'The 6-digit code from your email',
    },
  ],
  async handler({ options, ctx, principal, discordUserId }) {
    const result = await ctx.emailService.confirmAndAddEmail({
      personId: principal.person.id,
      discordUserId,
      code: options.code,
    });
    return renderVerifyEmailResult(result);
  },
});
