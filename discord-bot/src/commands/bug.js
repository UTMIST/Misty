/**
 * @file bug.js
 * @description Temporary dummy /bug command directing users where to report bugs.
 *
 * DEPRECATION / MIGRATION GUIDE:
 * This command serves as a lightweight placeholder directing users to GitHub
 * issues (with an optional Discord infrastructure contact) until a full in-bot
 * bug reporting workflow (e.g. interactive modals, issue intake integration,
 * automated triage) is implemented.
 *
 * How to deprecate or replace this command in the future:
 * 1. To replace with an interactive bug submission flow:
 *    - Update this command handler to trigger a Discord modal interaction or modal form.
 *    - Update the command options and documentation accordingly.
 * 2. To retire this command completely:
 *    - Remove this file (`discord-bot/src/commands/bug.js`).
 *    - Remove the `bug` import and map entry in `discord-bot/src/commands/index.js`.
 *    - Remove `infrastructureDiscordUsername` from `src/config.js` and `src/context.js`,
 *      and clean up `INFRASTRUCTURE_DISCORD_USERNAME` in `.env.example` and docs.
 *    - Remove `discord-bot/test/bug-command.test.js`.
 *    - Re-run Discord command registration (`npm run register`) to drop the
 *      command from Discord globally.
 */

import { defineCommand } from '../defineCommand.js';

export const BUG_REPORT_URL = 'https://github.com/UTMIST/Misty/issues/new/choose';

/**
 * Format a Discord contact as a mention (<@snowflake>) when given a numeric user ID,
 * or as a plain handle (@username) when given a name or handle.
 *
 * @param {string} contact Discord username or numeric snowflake ID.
 * @returns {string} Formatted mention or handle string.
 */
export function formatContact(contact) {
  const cleaned = contact.trim().replace(/^@/, '');
  return /^\d+$/.test(cleaned) ? `<@${cleaned}>` : `@${cleaned}`;
}

/**
 * Build the reply message for /bug.
 *
 * Modular message formatting helper decoupled from the command handler
 * for easy unit testing, reuse, or future refactoring.
 *
 * @param {string|undefined|null} [contact] Optional Discord contact handle or user ID.
 * @returns {string} Formatted markdown message content.
 */
export function buildBugReportMessage(contact) {
  const baseMessage = `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}`;
  if (!contact || !contact.trim()) {
    return baseMessage;
  }
  return `${baseMessage}\nOr reach out to ${formatContact(contact)} on Discord.`;
}

export default defineCommand({
  name: 'bug',
  description: 'Where to report a bug',
  auth: 'public',
  beta: false,
  options: [],
  async handler({ ctx }) {
    return {
      content: buildBugReportMessage(ctx?.infrastructureDiscordUsername),
      ephemeral: true,
    };
  },
});
