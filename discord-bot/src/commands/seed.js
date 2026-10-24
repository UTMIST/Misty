import { defineCommand } from '../defineCommand.js';
import { renderSeedResult } from '../messages/people.js';

export default defineCommand({
  name: 'seed',
  description: 'Add a member to the directory (admins only)',
  auth: 'admin',
  beta: false,
  options: [
    { name: 'email', type: 'string', required: true, description: 'Member email' },
    { name: 'name', type: 'string', required: true, description: 'Display name' },
    {
      name: 'level',
      type: 'string',
      required: false,
      description: 'Access level (default member)',
      choices: [
        { name: 'member', value: 'member' },
        { name: 'admin', value: 'admin' },
        { name: 'superuser', value: 'superuser' },
      ],
    },
  ],
  async handler({ options, principal, ctx }) {
    const level = options.level ?? 'member';
    const result = await ctx.seedService.seedPerson(
      { email: options.email, displayName: options.name, level },
      { caller: principal.person },
    );
    return renderSeedResult(result);
  },
});
