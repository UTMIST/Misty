import { defineCommand } from '../defineCommand.js';

export default defineCommand({
  name: 'github',
  description: 'Connect, inspect, or unlink your verified GitHub account',
  auth: 'linked',
  beta: false,
  ephemeral: true,
  options: [
    {
      name: 'action',
      type: 'string',
      required: true,
      description: 'Reconnect uses the same account; unlink before switching accounts',
      choices: ['connect', 'reconnect', 'confirm', 'status', 'unlink'].map((value) => ({
        name: value,
        value,
      })),
    },
    {
      name: 'code',
      type: 'string',
      required: false,
      description: 'Private browser completion code for confirm',
    },
  ],
  async handler({ options, principal, discordUserId, ctx }) {
    if (!ctx.githubLinks) return { content: 'GitHub linking is not enabled.', ephemeral: true };
    const action = options.action;
    if (!['connect', 'reconnect', 'confirm', 'status', 'unlink'].includes(action)) {
      return { content: 'Choose connect, reconnect, confirm, status, or unlink.', ephemeral: true };
    }
    if (action === 'confirm' && !options.code) {
      return {
        content: 'Enter the completion code shown in your GitHub callback page.',
        ephemeral: true,
      };
    }
    try {
      const result = await ctx.githubLinks.request(
        ['connect', 'reconnect'].includes(action) ? 'start' : action,
        principal.person.id,
        discordUserId,
        ...(action === 'confirm' ? [options.code] : []),
      );
      if (result.authorize_url) {
        return {
          content: `Authorize Misty on GitHub: <${result.authorize_url}>\nThis link expires in five minutes. Complete authorization using the private code shown in your browser.`,
          ephemeral: true,
        };
      }
      if (result.status === 'linked') {
        return {
          content: `Verified GitHub account: **${String(result.login).replace(/[^a-zA-Z0-9-]/g, '')}**.`,
          ephemeral: true,
        };
      }
      const messages = {
        awaiting_confirmation:
          'Finish linking with /github action:confirm and the private code shown in your browser.',
        unlinked: 'No verified GitHub account is linked. Use connect to authorize Misty.',
        revoked: 'GitHub authorization was revoked. Use reconnect to authorize again.',
        reauthorization_required:
          'GitHub authorization expired or could not be refreshed. Use reconnect.',
      };
      return {
        content: messages[result.status] ?? 'GitHub linking unavailable. Try status again.',
        ephemeral: true,
      };
    } catch {
      return {
        content:
          'GitHub linking unavailable. Check status before reconnecting; unlink before switching accounts.',
        ephemeral: true,
      };
    }
  },
});
