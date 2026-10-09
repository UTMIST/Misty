import { defineCommand } from '../defineCommand.js';
import { authorize } from '../auth/policy.js';
import { buildCommandDetailEmbed, buildHelpEmbed, helpMessages } from '../messages/help.js';

/**
 * Reduce a command's declarative authorization rule to a policy that can be
 * evaluated without an invocation intent.
 *
 * @param {object} command Neutral command or subcommand metadata.
 * @returns {'public'|'linked'|'admin'|'superuser'} Policy used for help visibility.
 */
function visiblePolicy(command) {
  // Dynamic policies depend on request options. Treat them as linked for menu
  // visibility, matching the command registry's fail-secure convention.
  return typeof command.auth === 'function' ? 'linked' : command.auth;
}

export default defineCommand({
  name: 'help',
  description: 'List commands you can use or show details for one command',
  auth: 'public',
  identifyCaller: true,
  beta: false,
  options: [
    {
      name: 'command',
      type: 'string',
      required: false,
      description: 'Command name to show details for',
    },
  ],
  /**
   * Render the command list or details for one command, filtered to the caller.
   *
   * @param {object} invocation Surface-neutral command invocation.
   * @returns {Promise<object>} Ephemeral reply payload.
   */
  async handler({ options, principal, ctx }) {
    const visible = [...ctx.commands.values()]
      .filter((command) => command.name !== 'help')
      .filter((command) => authorize(visiblePolicy(command), principal).ok);

    if (!options.command) return buildHelpEmbed(visible);

    const requested = options.command.trim().replace(/^\//, '').toLowerCase();
    const command = visible.find((candidate) => candidate.name === requested);
    if (!command) return helpMessages.unknownCommand(requested);

    return buildCommandDetailEmbed({
      ...command,
      subcommands: command.subcommands.filter(
        (subcommand) => authorize(visiblePolicy(subcommand), principal).ok,
      ),
    });
  },
});
