import { defineCommand } from '../defineCommand.js';

function renderLimits(limits) {
  return {
    content: `Helper limit: **${limits.maxRequests}** requests per **${limits.windowSeconds}** seconds.`,
    ephemeral: true,
  };
}

export default defineCommand({
  name: 'helper-limits',
  description: 'View or change the helper request allowance (admins only)',
  auth: 'admin',
  subcommands: [
    {
      name: 'show',
      description: 'Show the current helper request allowance',
      auth: 'admin',
      options: [],
      async handler({ ctx }) {
        return renderLimits(ctx.helperRequestLimiter.getLimits());
      },
    },
    {
      name: 'set',
      description: 'Change the helper request allowance without resetting usage',
      auth: 'admin',
      options: [
        {
          name: 'max_requests',
          type: 'string',
          required: false,
          description: 'Positive whole-number request limit',
        },
        {
          name: 'window_seconds',
          type: 'string',
          required: false,
          description: 'Positive whole-number window duration in seconds',
        },
      ],
      async handler({ options, ctx }) {
        const maxRequests = options.max_requests;
        const windowSeconds = options.window_seconds;
        const hasMaxRequests = maxRequests !== null && maxRequests !== undefined;
        const hasWindowSeconds = windowSeconds !== null && windowSeconds !== undefined;
        if (!hasMaxRequests && !hasWindowSeconds) {
          return {
            content: 'Provide max_requests, window_seconds, or both.',
            ephemeral: true,
          };
        }
        const updates = {};
        if (hasMaxRequests) updates.maxRequests = Number(maxRequests);
        if (hasWindowSeconds) updates.windowSeconds = Number(windowSeconds);
        try {
          return renderLimits(ctx.helperRequestLimiter.updateLimits(updates));
        } catch (error) {
          return {
            content: `Invalid helper limit: ${error.message}.`,
            ephemeral: true,
          };
        }
      },
    },
  ],
  async handler(intent) {
    const sub = this.subcommands.find((s) => s.name === intent.subcommand);
    if (!sub) return { content: 'Something went wrong. Please try again.', ephemeral: true };
    return sub.handler(intent);
  },
});
