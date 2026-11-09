import { defineCommand } from '../defineCommand.js';
import { renderPromoteResult } from '../messages/people.js';

export default defineCommand({
  name: 'promote',
  description: "Promote a lower-ranked member's global access level (admins only)",
  auth: 'admin',
  beta: false,
  options: [
    { name: 'user', type: 'user', required: true, description: 'Discord user' },
    {
      name: 'level',
      type: 'string',
      required: true,
      description: 'New global access level (not a team role)',
      choices: [
        { name: 'admin', value: 'admin' },
        { name: 'superuser', value: 'superuser' },
      ],
    },
  ],
  async handler({ options, principal, ctx }) {
    // The caller is the router's freshly authenticated principal; its level is
    // never taken from options.
    const result = await ctx.promotionService.promote({
      caller: principal.person,
      personId: options.user.personId,
      discordSnowflake: options.user.id,
      level: options.level,
      expectedPerson: options.expectedPerson,
      beforeWrite: options.beforePersonWrite,
    });
    if (options.expectedPerson && result.outcome === 'UPDATE_UNCERTAIN') {
      throw new Error('Promotion result is uncertain. Check current state before asking again.');
    }
    if (result.outcome === 'PROMOTED') options.onPromoted?.();
    return renderPromoteResult(result);
  },
});
