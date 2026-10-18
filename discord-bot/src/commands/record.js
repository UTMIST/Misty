import { defineCommand } from '../defineCommand.js';

// Registration-only. This command exists so registerCommands.js registers the
// `/record` slash command (and its subcommands) with Discord. The
// interactionCreate handler in src/adapters/discord/index.js intercepts
// commandName === 'record' BEFORE the neutral dispatch path and routes
// straight to appContext.meetingSurface — the handlers below are never
// invoked in production. They exist only so defineCommand's validation
// (which requires a handler) is satisfied, and as a safety net if the
// interception is ever bypassed.
function unreachable() {
  return { content: 'Recording is handled by the voice surface.', ephemeral: true };
}

export default defineCommand({
  name: 'record',
  description: 'Record the current voice channel and post meeting minutes',
  auth: 'linked',
  beta: false, // stable → registered globally (all prod servers)
  options: [],
  subcommands: [
    {
      // Inherits the command-level 'linked' policy: starting a recording
      // consumes resources (a voice connection + a live meeting session), so it
      // must be authenticated.
      name: 'start',
      description: 'Start recording the voice channel you are in',
      options: [
        {
          name: 'name',
          type: 'string',
          required: false,
          description: 'Name for the meeting minutes',
          maxLength: 100,
        },
        {
          name: 'subtitles',
          type: 'boolean',
          required: false,
          description: 'Post live subtitles in a thread (default: on)',
        },
      ],
      handler: unreachable,
    },
    {
      // Read-only, resolved from local session state (no directory call), so a
      // directory outage must not break it -> 'public'.
      name: 'status',
      auth: 'public',
      description: 'Check whether a recording is in progress',
      handler: unreachable,
    },
    {
      // De-escalating: stopping a recording bounds resource/memory use, so it
      // must NOT be gated behind a check that can fail-closed during a directory
      // outage (that would strand a running recording) -> 'public'.
      name: 'stop',
      auth: 'public',
      description: 'Stop recording and post the meeting minutes',
      handler: unreachable,
    },
  ],
  async handler() {
    return unreachable();
  },
});
