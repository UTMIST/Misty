import { commands } from './commands/index.js';
import { authorizeCommand, dispatch } from './router.js';
import { renderActionSummary } from './messages/helperActions.js';

export const llmSafe = true;

// Deliberate allowlist. Adding a slash command never automatically gives the
// model a new capability. Identity/email verification stays outside this list.
const OPERATIONS = [
  ['team', 'list'],
  ['team', 'roster'],
  ['my-teams'],
  ['whoami'],
  ['help'],
  ['bug'],
  ['doc', 'list'],
  ['doc', 'add', true],
  ['doc', 'remove', true],
  ['team', 'create', true],
  ['team', 'rename', true],
  ['team', 'add', true],
  ['team', 'remove', true],
  ['seed', null, true],
  ['team', 'google-status'],
  ['team', 'google-sync', true],
  ['channel-access', 'show'],
  ['channel-access', 'set', true],
  ['channel-access', 'append', true],
  ['channel-access', 'clear', true],
  ['helper-limits', 'show'],
  ['helper-limits', 'set', true],
  ['record', 'status'],
  ['record', 'start', true],
  ['record', 'stop', true],
  ['qna'],
];

// Reply visibility is independent of slash-command authentication/ephemerality.
const PUBLIC_READS = new Set(['team_list', 'team_roster', 'help', 'bug']);

function optionSchema(option) {
  if (option.type === 'boolean') return { type: 'boolean', description: option.description };
  return {
    type: 'string',
    description:
      option.type === 'user'
        ? 'Discord user ID from an explicit user mention. Ask for a mention if unknown.'
        : option.description,
    minLength: 1,
    maxLength: option.maxLength ?? 1000,
    ...(option.type === 'user' && { pattern: '^[0-9]{1,20}$' }),
    ...(option.choices && { enum: option.choices.map((choice) => choice.value) }),
  };
}

function validatedOptions(input, options) {
  if (
    !input ||
    typeof input !== 'object' ||
    Array.isArray(input) ||
    JSON.stringify(input).length > 4000
  ) {
    throw new Error('Invalid tool arguments.');
  }
  if (Object.keys(input).some((key) => !options.some((o) => o.name === key))) {
    throw new Error('Unexpected tool argument.');
  }
  const result = {};
  for (const option of options) {
    const value = input[option.name];
    if (value === undefined) {
      if (option.required) throw new Error(`Missing ${option.name}.`);
      continue;
    }
    const schema = optionSchema(option);
    if (
      typeof value !== schema.type ||
      (typeof value === 'string' &&
        (!value.trim() ||
          [...value].length > schema.maxLength ||
          (schema.pattern && !new RegExp(schema.pattern).test(value)) ||
          (schema.enum && !schema.enum.includes(value))))
    ) {
      throw new Error(`Invalid ${option.name}.`);
    }
    result[option.name] = option.type === 'user' ? Object.freeze({ id: value }) : value;
  }
  return Object.freeze(result);
}

/** Tools prepare terminal UI actions; command results never enter model history. */
export function createCommandTools({ appContext }) {
  return OPERATIONS.map(([commandName, subcommand = null, confirm = false]) => {
    const command = commands.get(commandName);
    const operation = subcommand ? command.subcommands.find((s) => s.name === subcommand) : command;
    const name = [commandName, subcommand].filter(Boolean).join('_').replaceAll('-', '_');
    const delivery = PUBLIC_READS.has(name)
      ? 'Replies publicly in server conversations'
      : commandName === 'record'
        ? 'Recording status and successful changes may be announced only in the meeting origin channel; other results are private'
        : 'Delivers a private result';
    return {
      definition: {
        name,
        description: `${operation.description}. Authenticates the requesting user. ${delivery}${confirm ? ' after private user confirmation' : ''}. Include all requested actions in the same tool-call response (one call per member for team changes, at most four calls total including lookups). The whole batch ends the assistant turn; results are not returned to the model.`,
        input_schema: {
          type: 'object',
          additionalProperties: false,
          properties: Object.fromEntries(operation.options.map((o) => [o.name, optionSchema(o)])),
          required: operation.options.filter((o) => o.required).map((o) => o.name),
        },
      },
      async run(input, { request, principal, withDeadline = (fn) => fn() }) {
        let options;
        try {
          options = validatedOptions(input, operation.options);
        } catch (error) {
          return { is_error: true, content: { error: error.message } };
        }
        if (!request?.discordUserId || !principal?.person?.id) {
          return { is_error: true, content: { error: 'Authenticated requester is required.' } };
        }
        let expiresAt = Date.now() + 120000;
        const intent = Object.freeze({
          surface: 'discord',
          commandName,
          subcommand,
          options,
          discordUserId: request.discordUserId,
          discordHandle: request.discordHandle,
          discordGuildId: request.discordGuildId,
          discordChannelId: request.discordChannelId,
          channelUsedParent: Boolean(request.channelUsedParent),
          requireLinked: true,
          expectedPersonId: principal.person.id,
          get expiresAt() {
            return expiresAt;
          },
        });
        const check = async () => {
          if (!appContext.helperToolsEnabled) throw new Error('Helper tools are disabled.');
          const result = await authorizeCommand(intent, { commands, appContext });
          if (result.error)
            throw new Error('Your account or permissions no longer authorize this action.');
          await request.checkSurface?.();
          if (Date.now() >= intent.expiresAt) throw new Error('This action expired. Ask again.');
          return result.principal;
        };
        try {
          await withDeadline(check);
        } catch {
          return {
            is_error: true,
            content: { error: 'This action is not available to your account.' },
          };
        }
        // No model data can override the author, guild, channel or meeting target.
        const runMeeting = request.runMeeting;
        const publicRead = Boolean(
          request.discordGuildId &&
          (PUBLIC_READS.has(name) ||
            (name === 'record_status' &&
              request.recordingTextChannelId &&
              request.recordingTextChannelId === request.sourceChannelId)),
        );
        // Resolve display labels only after authorization, without performing a write.
        // The preview and execution both use the frozen, validated options.
        const people = {};
        for (const option of operation.options) {
          if (option.type === 'user' && options[option.name]) {
            const id = options[option.name].id;
            people[id] = await withDeadline(() => appContext.directory.getPersonByDiscordId(id));
          }
        }
        const teamSlug = options.team ?? (commandName === 'team' ? options.slug : null);
        const team =
          teamSlug && !(commandName === 'team' && subcommand === 'create')
            ? await withDeadline(() => appContext.directory.getTeamBySlug(teamSlug))
            : null;
        return {
          content: {
            status: confirm
              ? 'confirmation_required'
              : publicRead
                ? 'public_result'
                : 'private_result',
          },
          action: {
            userId: intent.discordUserId,
            confirm,
            publicRead,
            summary: renderActionSummary({
              commandName,
              subcommand,
              operation,
              options,
              request,
              people,
              team,
            }),
            check,
            setExpiresAt(value) {
              expiresAt = value;
            },
            async execute() {
              await check();
              if (commandName === 'record' || commandName === 'qna') {
                if (!runMeeting) return { content: 'Use this action in a server channel.' };
                return runMeeting({ commandName, subcommand, options });
              }
              const payload = await dispatch(intent, { commands, appContext });
              return publicRead ? { ...payload, publicReply: { payload } } : payload;
            },
          },
        };
      },
    };
  });
}
