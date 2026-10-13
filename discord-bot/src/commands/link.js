import { defineCommand } from '../defineCommand.js';
import { renderLinkResult } from '../messages/link.js';

export default defineCommand({
  name: 'link',
  description: 'Link your Discord account to your UTMIST directory record',
  auth: 'public', // you are not linked yet when you run /link
  beta: false, // stable → registered globally (all prod servers)
  options: [
    { name: 'email', type: 'string', required: true, description: 'Your registered UTMIST email' },
  ],
  async handler({ options, ctx, discordUserId, discordHandle }) {
    const result = await ctx.linkService.linkByEmail({
      email: options.email,
      discordUserId,
      discordHandle,
    });
    return renderLinkResult(result);
  },
});
