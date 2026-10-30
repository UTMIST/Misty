import { commands } from './commands/index.js';
import { authorizeCommand, dispatch } from './router.js';
import { renderActionSummary } from './messages/helperActions.js';
import { checkOnce } from './helperChecks.js';
import { combineHelperActions, MAX_HELPER_ACTIONS } from './helperActions.js';

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
    const bulkMembers = commandName === 'team' && ['add', 'remove'].includes(subcommand);
    const delivery = PUBLIC_READS.has(name)
      ? 'Replies publicly in server conversations'
      : commandName === 'record'
        ? 'Recording status and successful changes may be announced only in the meeting origin channel; other results are private'
        : 'Delivers a private result';
    const tool = {
      terminal: true,
      actionCount: (input) => (bulkMembers && Array.isArray(input?.user) ? input.user.length : 1),
      definition: {
        name,
        description: `${operation.description}. Authenticates the requesting user. ${delivery}${confirm ? ' after private user confirmation' : ''}. Include ALL requested actions in the same tool-call response, up to ${MAX_HELPER_ACTIONS} actions total, separately from the four document-tool calls allowed. ${bulkMembers ? 'For multiple members with the same team and options, pass EVERY mentioned member ID as an array in user; do not stop after the first member. ' : ''}If the full request exceeds the action limit or lacks required details, ask the user to split or clarify it; never propose only a subset. Finish any document searches or reads in earlier tool rounds; never mix them with command calls in the same response. Prepared batches end the assistant turn; command results are not returned to the model. If every preparation fails, explain the errors or ask for missing details without further tool calls.`,
        input_schema: {
          type: 'object',
          additionalProperties: false,
          properties: Object.fromEntries(
            operation.options.map((o) => [
              o.name,
              bulkMembers && o.name === 'user'
                ? {
                    anyOf: [
                      optionSchema(o),
                      {
                        type: 'array',
                        minItems: 1,
                        maxItems: MAX_HELPER_ACTIONS,
                        uniqueItems: true,
                        items: optionSchema(o),
                      },
                    ],
                    description:
                      'One Discord user ID, or ALL explicitly mentioned member IDs for this team change.',
                  }
                : optionSchema(o),
            ]),
          ),
          required: operation.options.filter((o) => o.required).map((o) => o.name),
        },
      },
      async run(input, { request, principal, withDeadline = (fn) => fn() }) {
        if (bulkMembers && Array.isArray(input?.user)) {
          let members;
          try {
            if (
              !input.user.length ||
              input.user.length > MAX_HELPER_ACTIONS ||
              new Set(input.user).size !== input.user.length ||
              JSON.stringify(input).length > 4000
            ) {
              throw new Error(`Supply 1–${MAX_HELPER_ACTIONS} distinct member IDs.`);
            }
            // Validate and copy the entire list before any authorization/label I/O.
            members = input.user.map((user) => {
              const member = { ...input, user };
              validatedOptions(member, operation.options);
              return member;
            });
          } catch (error) {
            return { is_error: true, content: { error: error.message } };
          }
          const actions = [];
          for (const member of members) {
            const result = await tool.run(member, { request, principal, withDeadline });
            if (result.is_error || !result.action) return result;
            actions.push(result.action);
          }
          return {
            content: { status: 'confirmation_required' },
            action: combineHelperActions(actions),
          };
        }
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
          sharedHelp: commandName === 'help' && Boolean(request.discordGuildId),
          expectedPersonId: principal.person.id,
          get expiresAt() {
            return expiresAt;
          },
        });
        const check = async (checks = new Map()) =>
          checkOnce(checks, check, async () => {
            if (!appContext.helperToolsEnabled) throw new Error('Helper tools are disabled.');
            const result = await authorizeCommand(intent, { commands, appContext, checks });
            if (result.error)
              throw new Error('Your account or permissions no longer authorize this action.');
            if (request.checkSurface)
              await checkOnce(checks, request.checkSurface, request.checkSurface);
            if (Date.now() >= intent.expiresAt) throw new Error('This action expired. Ask again.');
            return result.principal;
          });
        try {
          await withDeadline(() => check());
        } catch {
          return {
            is_error: true,
            content: { error: 'This action is not available to your account.' },
          };
        }
        // No model data can override the author, guild, channel or meeting target.
        const runMeeting = request.runMeeting;
        // Preparation decides whether to open a private interaction. The
        // recording adapter rechecks the exact origin/session when publishing.
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
        const readLabel = (read) =>
          withDeadline(async () => {
            try {
              return await read();
            } catch {
              return null;
            }
          });
        for (const option of operation.options) {
          if (option.type === 'user' && options[option.name]) {
            const id = options[option.name].id;
            people[id] = await readLabel(() => appContext.directory.getPersonByDiscordId(id));
          }
        }
        const teamSlug = options.team ?? (commandName === 'team' ? options.slug : null);
        const team =
          teamSlug && !(commandName === 'team' && subcommand === 'create')
            ? await readLabel(() => appContext.directory.getTeamBySlug(teamSlug))
            : null;
        const action = {
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
          async execute(checks = new Map()) {
            await action.check(checks);
            if (commandName === 'record' || commandName === 'qna') {
              if (!runMeeting) return { content: 'Use this action in a server channel.' };
              return runMeeting({ commandName, subcommand, options }, checks);
            }
            const payload = await dispatch(intent, { commands, appContext, checks });
            return publicRead ? { ...payload, publicReply: { payload } } : payload;
          },
        };
        return {
          content: {
            status: confirm
              ? 'confirmation_required'
              : publicRead
                ? 'public_result'
                : 'private_result',
          },
          action,
        };
      },
    };
    return tool;
  });
}
