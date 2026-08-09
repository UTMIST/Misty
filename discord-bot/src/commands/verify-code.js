import { defineCommand } from '../defineCommand.js';
import { renderVerifyCodeResult } from '../messages.js';

export default defineCommand({
  name: 'verify-code',
  description: 'Confirm the code emailed to you to finish linking',
  auth: 'public', // you are not linked yet when you run /verify-code
  beta: false, // stable → registered globally (all prod servers)
  options: [
    {
      name: 'code',
      type: 'string',
      required: true,
      description: 'The 6-digit code from your email',
    },
  ],
  async handler({ options, ctx, discordUserId, discordHandle }) {
    const result = await ctx.linkService.confirmAndLink({
      discordUserId,
      discordHandle,
      code: options.code,
    });
    return renderVerifyCodeResult(result);
  },
});
