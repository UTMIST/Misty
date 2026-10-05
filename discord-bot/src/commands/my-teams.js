import { defineCommand } from '../defineCommand.js';
import { renderMyTeamsResult } from '../messages/team.js';

export default defineCommand({
  name: 'my-teams',
  description: 'List the teams you are on',
  auth: 'linked',
  beta: false,
  options: [],
  async handler({ principal, ctx }) {
    const caller = principal.person;
    const result = await ctx.teamService.getMyTeams({ personId: caller.id }, { caller });
    return renderMyTeamsResult(result);
  },
});
