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
  async handler() {
    return null;
  },
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
        if (options.max_requests === undefined && options.window_seconds === undefined) {
          return {
            content: 'Provide max_requests, window_seconds, or both.',
            ephemeral: true,
          };
        }
        const updates = {};
        if (options.max_requests !== undefined) updates.maxRequests = Number(options.max_requests);
        if (options.window_seconds !== undefined) {
          updates.windowSeconds = Number(options.window_seconds);
        }
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
});
