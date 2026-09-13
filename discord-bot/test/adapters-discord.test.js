import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MessageFlags } from 'discord.js';
import { defineCommand } from '../src/defineCommand.js';
import {
  interactionToIntent,
  interactionToAutocompleteIntent,
  payloadToDiscordReply,
  resolveEphemeral,
  wireDiscordClient,
  createAutoStop,
  createMeetingPrompt,
} from '../src/adapters/discord.js';
import { DirectoryUnavailable } from '../src/directoryClient.js';
import record from '../src/commands/record.js';

// A minimal fake discord.js interaction that tracks the response lifecycle the
// way the real one does: deferReply() flips `deferred`, reply() flips `replied`.
function fakeInteraction({ commandName, subcommand = null, calls }) {
  const interaction = {
    commandName,
    user: { id: '1', username: 'alex' },
    deferred: false,
    replied: false,
    isChatInputCommand: () => true,
    isAutocomplete: () => false,
    options: {
      getString: () => null,
      getBoolean: () => null,
      getUser: () => null,
      getSubcommand: () => subcommand,
    },
    async deferReply(opts) {
      calls.push({ method: 'deferReply', opts });
      this.deferred = true;
    },
    async reply(payload) {
      calls.push({ method: 'reply', payload });
      this.replied = true;
    },
    async editReply(payload) {
      calls.push({ method: 'editReply', payload });
    },
    async followUp(payload) {
      calls.push({ method: 'followUp', payload });
    },
  };
  return interaction;
}

// Minimal EventEmitter-ish client stub capturing the interactionCreate handler.
function fakeClient() {
  const handlers = new Map();
  return {
    on: (event, fn) => {
      handlers.set(event, fn);
    },
    emit: (interaction) => handlers.get('interactionCreate')?.(interaction),
    emitVoiceStateUpdate: (oldState, newState) =>
      handlers.get('voiceStateUpdate')?.(oldState, newState),
  };
}

const linkLike = defineCommand({
  name: 'link',
  description: 'x',
  options: [{ name: 'email', type: 'string', required: true, description: 'x' }],
  handler: async () => ({ content: 'ok', ephemeral: true }),
});

test('interactionToIntent extracts flat options', () => {
  const interaction = {
    commandName: 'link',
    guildId: 'guild-1',
    user: { id: '1', username: 'alex' },
    options: {
      getString: (n) => (n === 'email' ? 'a@x' : null),
      getSubcommand: () => null,
    },
  };
  const intent = interactionToIntent(interaction, linkLike);
  assert.equal(intent.commandName, 'link');
  assert.equal(intent.surface, 'discord');
  assert.equal(intent.discordGuildId, 'guild-1');
  assert.equal(intent.options.email, 'a@x');
  assert.equal(intent.discordUserId, '1');
  assert.equal(intent.discordHandle, 'alex');
  assert.equal(intent.subcommand, null);
});

test('interactionToIntent extracts subcommand', () => {
  const teamLike = defineCommand({
    name: 'team',
    description: 't',
    subcommands: [
      { name: 'list', options: [], handler: async () => ({ content: 'l' }) },
      {
        name: 'create',
        options: [{ name: 'slug', type: 'string', required: true, description: 's' }],
        handler: async () => ({ content: 'c' }),
      },
    ],
    handler: async () => ({ content: '?' }),
  });
  const interaction = {
    commandName: 'team',
    user: { id: '1', username: 'alex' },
    options: {
      getString: (n) => (n === 'slug' ? 'ml' : null),
      getSubcommand: () => 'create',
    },
  };
  const intent = interactionToIntent(interaction, teamLike);
  assert.equal(intent.subcommand, 'create');
  assert.equal(intent.options.slug, 'ml');
});

test('payloadToDiscordReply converts content + ephemeral', () => {
  const out = payloadToDiscordReply({ content: 'hi', ephemeral: true });
  assert.equal(out.content, 'hi');
  assert.equal(out.flags, MessageFlags.Ephemeral);
});

test('payloadToDiscordReply passes embeds through', () => {
  const spec = { title: 'X', fields: [{ name: 'a', value: 'b' }] };
  const out = payloadToDiscordReply({ embeds: [spec], ephemeral: true });
  assert.equal(out.embeds.length, 1);
  // discord.js can accept plain objects; we don't need EmbedBuilder in the adapter
  // for basic titles/fields.
  assert.equal(out.embeds[0].title, 'X');
});

test('payloadToDiscordReply returns null for null/undefined payload', () => {
  assert.equal(payloadToDiscordReply(null), null);
  assert.equal(payloadToDiscordReply(undefined), null);
});

test('payloadToDiscordReply propagates empty-string content', () => {
  const out = payloadToDiscordReply({ content: '' });
  assert.equal(out.content, '');
});

test('resolveEphemeral falls back to command-level hint', () => {
  const cmd = defineCommand({
    name: 'whoami',
    description: 'x',
    ephemeral: true,
    handler: async () => ({ content: 'ok' }),
  });
  assert.equal(resolveEphemeral(cmd, null), true);
});

test('resolveEphemeral prefers the active subcommand hint', () => {
  const cmd = defineCommand({
    name: 'team',
    description: 'x',
    ephemeral: false,
    subcommands: [{ name: 'roster', ephemeral: true, handler: async () => ({ content: 'ok' }) }],
    handler: async () => ({ content: 'top' }),
  });
  assert.equal(resolveEphemeral(cmd, 'roster'), true);
  assert.equal(resolveEphemeral(cmd, null), false);
});

test('wireDiscordClient defers BEFORE running the handler (slow DB safe)', async () => {
  const calls = [];
  let handlerRan = false;
  let deferredWhenHandlerRan = null;
  const command = defineCommand({
    name: 'whoami',
    description: 'x',
    auth: 'public',
    ephemeral: true,
    handler: async () => {
      handlerRan = true;
      // Simulate a slow Neon cold-start query; capture defer state at this point.
      deferredWhenHandlerRan = calls.some((c) => c.method === 'deferReply');
      return { content: 'record', ephemeral: true };
    },
  });
  const commands = new Map([['whoami', command]]);
  const client = fakeClient();
  wireDiscordClient(client, { commands, appContext: {} });

  const interaction = fakeInteraction({ commandName: 'whoami', calls });
  await client.emit(interaction);

  assert.equal(handlerRan, true);
  assert.equal(deferredWhenHandlerRan, true, 'handler must run only AFTER deferReply');
  assert.equal(calls[0].method, 'deferReply', 'deferReply must be the very first call');
});

test('wireDiscordClient defers ephemerally per the resolved hint', async () => {
  const calls = [];
  const command = defineCommand({
    name: 'whoami',
    description: 'x',
    auth: 'public',
    ephemeral: true,
    handler: async () => ({ content: 'record' }),
  });
  const client = fakeClient();
  wireDiscordClient(client, { commands: new Map([['whoami', command]]), appContext: {} });

  await client.emit(fakeInteraction({ commandName: 'whoami', calls }));

  const defer = calls.find((c) => c.method === 'deferReply');
  assert.equal(defer.opts.flags, MessageFlags.Ephemeral);
});

test('wireDiscordClient edits the deferred reply with the payload', async () => {
  const calls = [];
  const command = defineCommand({
    name: 'whoami',
    description: 'x',
    auth: 'public',
    ephemeral: true,
    handler: async () => ({ content: 'the record' }),
  });
  const client = fakeClient();
  wireDiscordClient(client, { commands: new Map([['whoami', command]]), appContext: {} });

  await client.emit(fakeInteraction({ commandName: 'whoami', calls }));

  const edit = calls.find((c) => c.method === 'editReply');
  assert.ok(edit, 'a deferred reply should be delivered via editReply, not a second reply');
  assert.equal(edit.payload.content, 'the record');
});

test('wireDiscordClient resolves the deferred reply with an error when dispatch throws', async () => {
  const calls = [];
  // A non-public command so dispatch runs the auth lookup. If that lookup throws
  // an unexpected error (e.g. a cold Neon DB failing mid-query), dispatch itself
  // throws — the router only catches errors from the handler, not from auth. The
  // interaction is already deferred, so the adapter must still resolve it or the
  // user is stuck staring at "thinking…" until the token expires.
  const command = defineCommand({
    name: 'whoami',
    description: 'x',
    auth: 'linked',
    ephemeral: true,
    handler: async () => ({ content: 'record' }),
  });
  const appContext = {
    directory: {
      getPersonByDiscordId: async () => {
        throw new Error('db exploded');
      },
    },
  };
  const client = fakeClient();
  wireDiscordClient(client, { commands: new Map([['whoami', command]]), appContext });

  await client.emit(fakeInteraction({ commandName: 'whoami', calls }));

  const edit = calls.find((c) => c.method === 'editReply');
  assert.ok(edit, 'a thrown dispatch must still resolve the deferred reply');
  assert.ok(
    edit.payload.content && edit.payload.content.length > 0,
    'the error reply should carry a user-facing message',
  );
});

function fakeAutocompleteInteraction({ commandName, subcommand, focused, calls }) {
  return {
    commandName,
    user: { id: 'u1', username: 'alex' },
    isAutocomplete: () => true,
    isChatInputCommand: () => false,
    options: {
      getFocused: (full) => (full ? focused : focused.value),
      getSubcommand: () => subcommand,
    },
    async respond(choices) {
      calls.push({ method: 'respond', choices });
    },
  };
}

test('interactionToAutocompleteIntent extracts focused option and typed value', () => {
  const intent = interactionToAutocompleteIntent(
    fakeAutocompleteInteraction({
      commandName: 'doc',
      subcommand: 'list',
      focused: { name: 'team', value: 'm' },
      calls: [],
    }),
  );
  assert.equal(intent.commandName, 'doc');
  assert.equal(intent.subcommand, 'list');
  assert.equal(intent.focusedOption, 'team');
  assert.equal(intent.typed, 'm');
  assert.equal(intent.discordUserId, 'u1');
});

test('wireDiscordClient responds to autocomplete with suggestions', async () => {
  const calls = [];
  const doc = defineCommand({
    name: 'doc',
    description: 'd',
    handler: async () => ({ content: 'x' }),
    subcommands: [
      {
        name: 'list',
        description: 'l',
        handler: async () => ({ content: 'y' }),
        options: [
          {
            name: 'team',
            type: 'string',
            description: 't',
            autocomplete: async () => [{ name: 'ML', value: 'ml' }],
          },
        ],
      },
    ],
  });
  const commands = new Map([['doc', doc]]);
  const appContext = { directory: { getPersonByDiscordId: async () => ({ id: 'p1' }) } };
  const client = fakeClient();
  wireDiscordClient(client, { commands, appContext });
  await client.emit(
    fakeAutocompleteInteraction({
      commandName: 'doc',
      subcommand: 'list',
      focused: { name: 'team', value: 'm' },
      calls,
    }),
  );
  const respond = calls.find((c) => c.method === 'respond');
  assert.deepEqual(respond.choices, [{ name: 'ML', value: 'ml' }]);
});

// --- /record auth enforcement (dedicated adapter path, bypasses the router PEP) ---

function fakeRecordInteraction({ subcommand, voiceChannel = null, calls }) {
  return {
    commandName: 'record',
    guildId: 'g1',
    user: { id: 'u1', username: 'alex' },
    channel: { id: 'tc1' },
    member: { voice: { channel: voiceChannel } },
    isChatInputCommand: () => true,
    isAutocomplete: () => false,
    options: { getSubcommand: () => subcommand },
    async deferReply(opts) {
      calls.push({ method: 'deferReply', opts });
    },
    async editReply(payload) {
      calls.push({ method: 'editReply', payload });
    },
  };
}

// The real `record` command in the registry so the adapter reads its per-
// subcommand auth (start='linked', status/stop='public') straight from metadata.
const recordCommands = new Map([['record', record]]);

test('/record start is denied for an UNLINKED caller and never starts recording', async () => {
  const calls = [];
  let startCalled = false;
  const appContext = {
    directory: { getPersonByDiscordId: async () => null },
    meetingSurface: {
      start: () => {
        startCalled = true;
        return { status: 'recording' };
      },
    },
  };
  const client = fakeClient();
  wireDiscordClient(client, { commands: recordCommands, appContext });

  await client.emit(
    fakeRecordInteraction({ subcommand: 'start', voiceChannel: { id: 'vc1' }, calls }),
  );

  const edit = calls.find((c) => c.method === 'editReply');
  assert.match(edit.payload.content, /link your account/i);
  assert.equal(startCalled, false, 'an unlinked caller must not reach meetingSurface.start');
});

test('/record start proceeds for a LINKED caller in a voice channel', async () => {
  const calls = [];
  let startArgs = null;
  const voiceChannel = { id: 'vc1' };
  const appContext = {
    directory: { getPersonByDiscordId: async () => ({ id: 'p1' }) },
    meetingSurface: {
      start: (args) => {
        startArgs = args;
        return { status: 'recording' };
      },
    },
  };
  const client = fakeClient();
  wireDiscordClient(client, { commands: recordCommands, appContext });

  await client.emit(fakeRecordInteraction({ subcommand: 'start', voiceChannel, calls }));

  assert.ok(startArgs, 'a linked caller should reach meetingSurface.start');
  assert.equal(startArgs.voiceChannel, voiceChannel);
  const edit = calls.find((c) => c.method === 'editReply');
  assert.match(edit.payload.content, /recording/i);
});

test('/record start fails closed (no start) when the directory is unavailable', async () => {
  const calls = [];
  let startCalled = false;
  const appContext = {
    directory: {
      getPersonByDiscordId: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
    meetingSurface: {
      start: () => {
        startCalled = true;
        return { status: 'recording' };
      },
    },
  };
  const client = fakeClient();
  wireDiscordClient(client, { commands: recordCommands, appContext });

  await client.emit(
    fakeRecordInteraction({ subcommand: 'start', voiceChannel: { id: 'vc1' }, calls }),
  );

  const edit = calls.find((c) => c.method === 'editReply');
  assert.match(edit.payload.content, /unavailable/i);
  assert.equal(startCalled, false);
});

test('/record stop is PUBLIC: an unlinked caller can still stop a runaway recording', async () => {
  const calls = [];
  let stopped = false;
  const appContext = {
    // A directory OUTAGE must not strand a live recording -- stop must not even
    // depend on the lookup. (Throw to prove stop never calls resolvePrincipal.)
    directory: {
      getPersonByDiscordId: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
    meetingSurface: {
      stop: async () => {
        stopped = true;
        return { status: 'stopped' };
      },
    },
  };
  const client = fakeClient();
  wireDiscordClient(client, { commands: recordCommands, appContext });

  await client.emit(fakeRecordInteraction({ subcommand: 'stop', calls }));

  assert.equal(stopped, true, 'stop is public and must proceed regardless of link/directory state');
});

test('/record status is PUBLIC: works for an unlinked caller without a directory call', async () => {
  const calls = [];
  const appContext = {
    directory: {
      getPersonByDiscordId: async () => {
        throw new DirectoryUnavailable('down');
      },
    },
    meetingSurface: { status: () => ({ status: 'not-recording' }) },
  };
  const client = fakeClient();
  wireDiscordClient(client, { commands: recordCommands, appContext });

  await client.emit(fakeRecordInteraction({ subcommand: 'status', calls }));

  const edit = calls.find((c) => c.method === 'editReply');
  assert.match(edit.payload.content, /no recording in progress/i);
});

// --- voice-state behavior ---

// The head-count reads guild.voiceStates.cache (populated by GuildVoiceStates),
// NOT channel.members (which needs the privileged GuildMembers intent). Model
// exactly that: a channel with an id + a guild whose voiceStates.cache lists the
// occupants of THIS channel as { id: userId, channelId, member }. `resolved:false`
// simulates a member the bot couldn't resolve (no GuildMembers intent) -- the
// exact case that made channel.members miscount and auto-stop never fire.
const BOT_ID = 'bot-1';
function fakeVoiceChannel(id, occupants) {
  const cache = new Map();
  for (const o of occupants) {
    cache.set(o.userId, {
      id: o.userId,
      channelId: id,
      member: o.resolved === false ? null : { user: { bot: !!o.bot } },
    });
  }
  return { id, guild: { id: 'g1', voiceStates: { cache } } };
}
const botOcc = { userId: BOT_ID, bot: true };
const botOccUnresolved = { userId: BOT_ID, resolved: false }; // member not cached
const humanOcc = (userId = 'h1') => ({ userId, bot: false });
const activeSessionOf = (sessionId, channelId, occupants) => ({
  sessionId,
  voiceChannel: fakeVoiceChannel(channelId, occupants),
});

// A controllable timer: setTimer captures the callback, fire() runs it.
function fakeTimer() {
  const state = { cb: null, cleared: 0 };
  return {
    setTimer: (fn) => {
      state.cb = fn;
      return 1;
    },
    clearTimer: () => {
      state.cleared += 1;
      state.cb = null;
    },
    fire: () => {
      const fn = state.cb;
      state.cb = null;
      if (fn) return fn();
    },
    get scheduled() {
      return state.cb !== null;
    },
    get cleared() {
      return state.cleared;
    },
  };
}

const makeAutoStop = (timer, meetingSurface) =>
  createAutoStop({
    meetingSurface,
    getBotId: () => BOT_ID,
    setTimer: timer.setTimer,
    clearTimer: timer.clearTimer,
  });

test('meeting prompt DMs the first human to enter an empty voice channel', () => {
  const sent = [];
  const user = { send: (payload) => sent.push(payload) };
  const voiceChannel = fakeVoiceChannel('vc1', [humanOcc('h1')]);
  const onPrompt = createMeetingPrompt({
    meetingSurface: { activeSession: () => null },
    getBotId: () => BOT_ID,
  });

  onPrompt(
    { channelId: null, guild: voiceChannel.guild },
    {
      id: 'h1',
      channelId: 'vc1',
      channel: voiceChannel,
      guild: voiceChannel.guild,
      member: { user },
    },
  );

  assert.deepEqual(sent, [
    {
      content:
        'A new meeting is starting. Run `/record start` to begin recording the voice channel.',
    },
  ]);
});

test('meeting prompt fetches the user when the voice-state member is uncached', async () => {
  const sent = [];
  const user = { send: (payload) => sent.push(payload) };
  const voiceChannel = fakeVoiceChannel('vc1', [humanOcc('h1')]);
  const onPrompt = createMeetingPrompt({
    meetingSurface: { activeSession: () => null },
    getBotId: () => BOT_ID,
    getUser: async (userId) => {
      assert.equal(userId, 'h1');
      return user;
    },
  });

  onPrompt(
    { channelId: null, guild: voiceChannel.guild },
    { id: 'h1', channelId: 'vc1', channel: voiceChannel, guild: voiceChannel.guild },
  );
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(sent.length, 1);
});

test('meeting prompt does not ping later entrants, bots, or an active meeting', () => {
  const sent = [];
  const laterVoiceChannel = fakeVoiceChannel('vc1', [humanOcc('h1'), humanOcc('h2')]);
  const onPrompt = createMeetingPrompt({
    meetingSurface: { activeSession: () => null },
    getBotId: () => BOT_ID,
  });

  onPrompt(
    { channelId: null, guild: laterVoiceChannel.guild },
    { id: 'h2', channelId: 'vc1', channel: laterVoiceChannel, guild: laterVoiceChannel.guild },
  );

  const botVoiceChannel = fakeVoiceChannel('vc1', [botOcc]);
  onPrompt(
    { channelId: null, guild: botVoiceChannel.guild },
    {
      id: BOT_ID,
      channelId: 'vc1',
      channel: botVoiceChannel,
      guild: botVoiceChannel.guild,
      member: { user: { bot: true } },
    },
  );

  const activeVoiceChannel = fakeVoiceChannel('vc1', [humanOcc('h3')]);
  const activePrompt = createMeetingPrompt({
    meetingSurface: { activeSession: () => ({ sessionId: 's1' }) },
    getBotId: () => BOT_ID,
  });
  activePrompt(
    { channelId: null, guild: activeVoiceChannel.guild },
    { id: 'h3', channelId: 'vc1', channel: activeVoiceChannel, guild: activeVoiceChannel.guild },
  );

  assert.deepEqual(sent, []);
});

test('wireDiscordClient runs the meeting prompt on voice-state updates', () => {
  const sent = [];
  const user = { send: (payload) => sent.push(payload) };
  const voiceChannel = fakeVoiceChannel('vc1', [humanOcc('h1')]);
  const client = fakeClient();
  client.user = { id: BOT_ID };
  wireDiscordClient(client, {
    commands: new Map(),
    appContext: { meetingSurface: { activeSession: () => null } },
  });

  client.emitVoiceStateUpdate(
    { channelId: null, guild: voiceChannel.guild },
    {
      id: 'h1',
      channelId: 'vc1',
      channel: voiceChannel,
      guild: voiceChannel.guild,
      member: { user },
    },
  );

  assert.equal(sent.length, 1);
  assert.match(sent[0].content, /new meeting/i);
});

// --- auto-stop when everyone leaves the recorded voice channel (debounced) ---

const leaveEvent = { channelId: 'vc1', guild: { id: 'g1' } };
const nowInVc2 = { channelId: 'vc2', guild: { id: 'g1' } };
const left = { channelId: null, guild: { id: 'g1' } };

test('auto-stop: the recorder bot alone does NOT count as an occupant (even if its member is uncached)', async () => {
  // Regression for the live bug: channel.members miscounted the bot without the
  // GuildMembers intent, so the channel never looked empty. Counting voice states
  // and excluding the bot by id fixes it -- even when the bot's member is null.
  const timer = fakeTimer();
  const meetingSurface = {
    activeSession: () => activeSessionOf('s1', 'vc1', [botOccUnresolved]),
    stop: async () => {},
  };
  makeAutoStop(timer, meetingSurface)(leaveEvent, left);
  assert.equal(
    timer.scheduled,
    true,
    'a channel with only the bot must read as empty and schedule a stop',
  );
});

test('auto-stop: DEBOUNCES -- schedules on empty, stops only after the grace timer fires', async () => {
  const timer = fakeTimer();
  let stopped = null;
  const meetingSurface = {
    activeSession: () => activeSessionOf('s1', 'vc1', [botOcc]), // only the bot
    stop: async (g) => {
      stopped = g;
      return { status: 'stopped' };
    },
  };
  makeAutoStop(timer, meetingSurface)(leaveEvent, left);
  assert.equal(timer.scheduled, true, 'stop must be SCHEDULED, not fired immediately');
  assert.equal(stopped, null);

  await timer.fire();
  assert.equal(stopped, 'g1');
});

test('auto-stop: cancels the pending stop if a human returns before the timer fires', async () => {
  const timer = fakeTimer();
  let stopCalled = false;
  let occupants = [botOcc]; // empty of humans
  const meetingSurface = {
    activeSession: () => activeSessionOf('s1', 'vc1', occupants),
    stop: async () => {
      stopCalled = true;
    },
  };
  const onVSU = makeAutoStop(timer, meetingSurface);

  onVSU(leaveEvent, left);
  assert.equal(timer.scheduled, true);

  occupants = [botOcc, humanOcc()]; // a human is back
  onVSU(left, { channelId: 'vc1', guild: { id: 'g1' } });
  assert.equal(timer.cleared, 1, 'a returning human must cancel the pending stop');
  assert.equal(timer.scheduled, false);

  await timer.fire(); // no-op: callback was cleared
  assert.equal(stopCalled, false);
});

test('auto-stop: re-checks at fire time and does NOT stop if a human is back', async () => {
  const timer = fakeTimer();
  let stopCalled = false;
  let occupants = [botOcc];
  const meetingSurface = {
    activeSession: () => activeSessionOf('s1', 'vc1', occupants),
    stop: async () => {
      stopCalled = true;
    },
  };
  const onVSU = makeAutoStop(timer, meetingSurface);

  onVSU(leaveEvent, left);
  occupants = [botOcc, humanOcc()]; // human back, but no cancelling event arrived
  await timer.fire();
  assert.equal(stopCalled, false, 'the fire-time re-check must bail when humans are present');
});

test('auto-stop: a stale timer from an ENDED session never stops a LATER recording in the guild', async () => {
  const timer = fakeTimer();
  let session = activeSessionOf('A', 'vc1', [botOcc]); // A, empty of humans
  const stops = [];
  const meetingSurface = {
    activeSession: () => session,
    stop: async (g) => {
      stops.push(g);
    },
  };
  const onVSU = makeAutoStop(timer, meetingSurface);

  onVSU(leaveEvent, left);
  assert.equal(timer.scheduled, true); // A's timer pending

  // A ends, B starts (same guild, empty). B's empty event must REPLACE A's stale
  // timer and schedule a fresh one bound to B.
  session = activeSessionOf('B', 'vc1', [botOcc]);
  onVSU(leaveEvent, left);
  assert.equal(timer.cleared, 1, "B's empty event must clear A's stale timer");
  assert.equal(timer.scheduled, true);

  await timer.fire();
  assert.deepEqual(stops, ['g1']);
});

test('auto-stop: covers the channel-MOVE case (member moves out, leaving it empty)', async () => {
  const timer = fakeTimer();
  const meetingSurface = {
    activeSession: () => activeSessionOf('s1', 'vc1', [botOcc]),
    stop: async () => {},
  };
  // Both channelIds non-null: the last human MOVED from vc1 (recorded) to vc2.
  makeAutoStop(timer, meetingSurface)(leaveEvent, nowInVc2);
  assert.equal(timer.scheduled, true);
});

test('auto-stop: does not schedule while humans remain', async () => {
  const timer = fakeTimer();
  const meetingSurface = {
    activeSession: () => activeSessionOf('s1', 'vc1', [botOcc, humanOcc()]),
    stop: async () => {},
  };
  makeAutoStop(timer, meetingSurface)(leaveEvent, left);
  assert.equal(timer.scheduled, false);
});

test('auto-stop: no-op (and clears any pending) when nothing is being recorded', async () => {
  const timer = fakeTimer();
  const meetingSurface = { activeSession: () => null, stop: async () => {} };
  makeAutoStop(timer, meetingSurface)(leaveEvent, left);
  assert.equal(timer.scheduled, false);
});
