import { commands } from './commands/index.js';
import { authorizeCommand, dispatch } from './router.js';

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
    return {
      definition: {
        name,
        description: `${operation.description}. Authenticates the requesting user. Delivers a private result${confirm ? ' after user confirmation' : ''}. Request one action at a time; this ends the assistant turn.`,
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
        return {
          content: { status: confirm ? 'confirmation_required' : 'private_result' },
          action: {
            userId: intent.discordUserId,
            confirm,
            summary: [
              operation.description,
              intent.discordGuildId
                ? `Server: ${request.guildLabel ?? ''} (${intent.discordGuildId})`
                : 'Direct message',
              intent.discordGuildId
                ? `Channel${intent.channelUsedParent ? ' (parent)' : ''}: ${intent.discordChannelId}`
                : null,
              intent.channelUsedParent ? `Request thread: ${request.sourceChannelId}` : null,
              commandName === 'record' && subcommand === 'start'
                ? `Recording location: your voice channel at confirmation. Minutes channel: ${request.sourceChannelId}`
                : null,
              commandName === 'record' && subcommand === 'start'
                ? `Save transcript and minutes as a Google Doc: ${(options.save ?? true) ? 'yes' : 'no'}`
                : null,
              commandName === 'record' && subcommand === 'stop'
                ? `Recording voice channel: ${request.recordingVoiceChannelId ?? 'no active recording'}. Applies only to the recording active when you asked.`
                : null,
              ...Object.entries(input).map(
                ([key, value]) => `${key.replaceAll('_', ' ')}: ${JSON.stringify(value)}`,
              ),
            ]
              .filter(Boolean)
              .join('\n')
              .replaceAll('`', '\\u0060'),
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
              return dispatch(intent, { commands, appContext });
            },
          },
        };
      },
    };
  });
}
