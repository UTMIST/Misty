import { defineCommand } from '../defineCommand.js';
import { myTeamsAutocomplete as teamAutocomplete } from './teamAutocomplete.js';
import {
  renderDocAddResult,
  renderDocListResult,
  renderDocShowResult,
  renderDocRemoveResult,
} from '../messages.js';

// Re-exported for callers/tests that reference /doc's autocomplete budget. The
// implementation now lives in the shared teamAutocomplete module.
export { AUTOCOMPLETE_TIMEOUT_MS } from './teamAutocomplete.js';

function parseTags(raw) {
  if (raw === null || raw === undefined) return [];
  return raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter((s) => s.length > 0);
}

export default defineCommand({
  name: 'doc',
  description: 'Catalog and look up UTMIST documents and links',
  auth: 'linked',
  beta: false, // stable → registered globally (all prod servers)
  options: [],
  subcommands: [
    {
      name: 'add',
      description: 'Catalog a URL in the doc registry',
      auth: 'linked',
      options: [
        { name: 'url', type: 'string', required: true, description: 'The link to catalog' },
        {
          name: 'title',
          type: 'string',
          required: false,
          description: 'Optional title (auto-fetched if omitted)',
        },
        {
          name: 'team',
          type: 'string',
          required: false,
          description: 'Owning team (slug)',
          autocomplete: teamAutocomplete,
        },
        { name: 'tags', type: 'string', required: false, description: 'Comma-separated tags' },
      ],
      async handler({ options, ctx, principal }) {
        const args = { url: options.url };
        if (options.title !== null && options.title !== undefined) args.title = options.title;
        if (options.team !== null && options.team !== undefined) args.teamSlug = options.team;
        args.tags = parseTags(options.tags);
        // The caller owns what they catalog, so it's always visible to them
        // (in addition to the owning team, if one was picked).
        args.owningPersonId = principal?.person?.id;
        const result = await ctx.docService.addDoc(args);
        return renderDocAddResult(result);
      },
    },
    {
      name: 'list',
      description: 'Browse the doc catalog',
      auth: 'linked',
      ephemeral: false,
      options: [
        {
          name: 'team',
          type: 'string',
          required: false,
          description: 'Filter by owning team (slug)',
          autocomplete: teamAutocomplete,
        },
        { name: 'tag', type: 'string', required: false, description: 'Filter by tag' },
        {
          name: 'source',
          type: 'string',
          required: false,
          description: 'Filter by source kind (e.g. gdocs, github)',
        },
      ],
      async handler({ options, ctx, principal }) {
        const args = {};
        if (options.team !== null && options.team !== undefined) args.teamSlug = options.team;
        if (options.tag !== null && options.tag !== undefined) args.tag = options.tag;
        if (options.source !== null && options.source !== undefined) args.source = options.source;
        // Read as the linked caller so the doc service filters to what they can see.
        args.onBehalfOf = principal?.person?.id;
        const result = await ctx.docService.listDocs(args);
        return renderDocListResult(result);
      },
    },
    {
      name: 'show',
      description: 'Show full detail for one doc',
      auth: 'linked',
      ephemeral: false,
      options: [
        { name: 'id', type: 'string', required: true, description: 'Doc id (from /doc list)' },
      ],
      async handler({ options, ctx, principal }) {
        const result = await ctx.docService.showDoc({
          id: options.id,
          onBehalfOf: principal?.person?.id,
        });
        return renderDocShowResult(result);
      },
    },
    {
      name: 'remove',
      description: 'Remove a doc from the catalog (admin)',
      auth: 'admin',
      options: [
        { name: 'id', type: 'string', required: true, description: 'Doc id (from /doc list)' },
      ],
      async handler({ options, ctx }) {
        const result = await ctx.docService.removeDoc({ id: options.id });
        return renderDocRemoveResult(result);
      },
    },
  ],
  async handler(intent) {
    const sub = this.subcommands.find((s) => s.name === intent.subcommand);
    if (!sub) return { content: 'Something went wrong. Please try again.', ephemeral: true };
    return sub.handler(intent);
  },
});
