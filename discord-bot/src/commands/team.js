import { defineCommand } from '../defineCommand.js';
import { allTeamsAutocomplete } from './teamAutocomplete.js';
import {
  renderCreateTeamResult,
  renderListTeamsResult,
  renderRenameTeamResult,
  renderAddMemberResult,
  renderRemoveMemberResult,
  renderRosterResult,
} from '../messages.js';

const ROLE_CHOICES = [
  { name: 'executive', value: 'executive' },
  { name: 'director', value: 'director' },
  { name: 'lead', value: 'lead' },
  { name: 'member', value: 'member' },
];

export default defineCommand({
  name: 'team',
  description: 'Manage UTMIST teams and memberships',
  auth: 'linked', // default; subcommands below override
  beta: false,
  options: [],
  subcommands: [
    {
      name: 'create',
      description: 'Create a new team (admin)',
      auth: 'admin',
      options: [
        { name: 'slug', type: 'string', required: true, description: 'Short handle, e.g. "ml"' },
        { name: 'label', type: 'string', required: true, description: 'Display name' },
        {
          name: 'description',
          type: 'string',
          required: false,
          description: 'Optional description',
        },
      ],
      async handler({ options, principal, ctx }) {
        const caller = principal?.person;
        const args = { slug: options.slug, label: options.label };
        if (options.description !== null && options.description !== undefined) {
          args.description = options.description;
        }
        const result = await ctx.teamService.createTeam(args, { caller });
        return renderCreateTeamResult(result);
      },
    },
    {
      name: 'list',
      description: 'List teams',
      auth: 'linked',
      ephemeral: false, // shared reference — post publicly so the channel can see it
      options: [
        {
          name: 'active_only',
          type: 'boolean',
          required: false,
          description: 'Only active teams (default true)',
        },
      ],
      async handler({ options, principal, ctx }) {
        const caller = principal?.person;
        const activeOnlyOpt = options.active_only;
        const activeOnly =
          activeOnlyOpt === null || activeOnlyOpt === undefined ? true : activeOnlyOpt;
        const result = await ctx.teamService.listTeams({ activeOnly }, { caller });
        return renderListTeamsResult(result);
      },
    },
    {
      name: 'rename',
      description: 'Rename a team (admin)',
      auth: 'admin',
      options: [
        {
          name: 'slug',
          type: 'string',
          required: true,
          description: 'Team slug',
          autocomplete: allTeamsAutocomplete,
        },
        { name: 'new_label', type: 'string', required: true, description: 'New display name' },
      ],
      async handler({ options, principal, ctx }) {
        const caller = principal?.person;
        const result = await ctx.teamService.renameTeam(
          { slug: options.slug, newLabel: options.new_label },
          { caller },
        );
        return renderRenameTeamResult(result);
      },
    },
    {
      name: 'add',
      description: 'Add a member to a team (admin)',
      auth: 'admin',
      options: [
        { name: 'user', type: 'user', required: true, description: 'Discord user' },
        {
          name: 'team',
          type: 'string',
          required: true,
          description: 'Team slug',
          autocomplete: allTeamsAutocomplete,
        },
        {
          name: 'role',
          type: 'string',
          required: false,
          description: 'Role (default member)',
          choices: ROLE_CHOICES,
        },
        {
          name: 'team_admin',
          type: 'boolean',
          required: false,
          description: 'Grant team-admin flag',
        },
      ],
      async handler({ options, principal, ctx }) {
        const caller = principal?.person;
        const args = {
          discordSnowflake: options.user.id,
          teamSlug: options.team,
        };
        if (options.role !== null && options.role !== undefined) args.roleKindId = options.role;
        if (options.team_admin !== null && options.team_admin !== undefined) {
          args.isTeamAdmin = options.team_admin;
        }
        const result = await ctx.teamService.addMember(args, { caller });
        return renderAddMemberResult(result);
      },
    },
    {
      name: 'remove',
      description: 'Remove a member from a team (admin)',
      auth: 'admin',
      options: [
        { name: 'user', type: 'user', required: true, description: 'Discord user' },
        {
          name: 'team',
          type: 'string',
          required: true,
          description: 'Team slug',
          autocomplete: allTeamsAutocomplete,
        },
      ],
      async handler({ options, principal, ctx }) {
        const caller = principal?.person;
        const result = await ctx.teamService.removeMember(
          { discordSnowflake: options.user.id, teamSlug: options.team },
          { caller },
        );
        return renderRemoveMemberResult(result);
      },
    },
    {
      name: 'roster',
      description: "Show a team's current roster",
      auth: 'linked',
      ephemeral: false, // shared reference — post publicly so the channel can see it
      options: [
        {
          name: 'team',
          type: 'string',
          required: true,
          description: 'Team slug',
          autocomplete: allTeamsAutocomplete,
        },
        { name: 'as_of', type: 'string', required: false, description: 'ISO date (default today)' },
      ],
      async handler({ options, principal, ctx }) {
        const caller = principal?.person;
        const args = { teamSlug: options.team };
        if (options.as_of !== null && options.as_of !== undefined) args.asOf = options.as_of;
        const result = await ctx.teamService.getRoster(args, { caller });
        return renderRosterResult(result);
      },
    },
  ],
  async handler(intent) {
    const sub = this.subcommands.find((s) => s.name === intent.subcommand);
    if (!sub) return { content: 'Something went wrong. Please try again.', ephemeral: true };
    return sub.handler(intent);
  },
});
