import { defineCommand } from '../defineCommand.js';

export const BUG_REPORT_URL = 'https://github.com/UTMIST/Misty/issues/new/choose';

export default defineCommand({
  name: 'bug',
  description: 'Report a bug to the infrastructure team',
  auth: 'public',
  beta: false,
  options: [],
  async handler({ ctx }) {
    return {
      content: `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}\nOr ping @${ctx.infrastructureDiscordUsername} in Discord.`,
      ephemeral: true,
    };
  },
});
