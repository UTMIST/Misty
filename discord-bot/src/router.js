import { resolvePrincipal } from './auth/principal.js';
import { authorize } from './auth/policy.js';
import { DirectoryUnavailable } from './directoryClient.js';
import { authMessages } from './messages.js';

/**
 * Select the commands registered on the surface where an intent originated.
 *
 * @param {Map<string, object>} commands Full application command registry.
 * @param {object} intent Surface-neutral invocation intent.
 * @param {object} appContext Application services and deployment metadata.
 * @returns {Map<string, object>} Registry available to the current invocation.
 */
function commandsAvailableToIntent(commands, intent, appContext) {
  if (intent.surface !== 'discord') return commands;
  const includeBeta = Boolean(
    appContext.discordGuildId && intent.discordGuildId === appContext.discordGuildId,
  );
  return new Map([...commands].filter(([, candidate]) => !candidate.beta || includeBeta));
}

// The single Policy Enforcement Point: authenticate -> authorize -> dispatch.
// Command handlers never re-implement any of this.
//
/**
 * Authenticate, authorize, and dispatch a surface-neutral command intent.
 *
 * @param {object} intent Command invocation metadata.
 * @param {object} dependencies Command registry and shared application context.
 * @returns {Promise<object|null>} Reply payload, or null for an unknown command.
 */
export async function dispatch(intent, { commands, appContext }) {
  const command = commands.get(intent.commandName);
  if (!command) return null;

  const activeSubcommand = intent.subcommand
    ? command.subcommands.find((s) => s.name === intent.subcommand)
    : null;
  const rawAuth = activeSubcommand?.auth ?? command.auth;
  const resolvedAuth = typeof rawAuth === 'function' ? rawAuth(intent) : rawAuth;
  const policy = resolvedAuth ?? 'linked'; // fail-secure default

  // --- Authentication ---
  let principal = null;
  if (policy !== 'public' || command.identifyCaller) {
    try {
      principal = await resolvePrincipal(appContext.directory, intent.discordUserId);
    } catch (e) {
      if (e instanceof DirectoryUnavailable) {
        // Optional identification must not turn a public command into an
        // unavailable one. It simply falls back to anonymous visibility.
        if (policy === 'public') principal = null;
        else return authMessages.unavailable(); // fail closed
      } else {
        throw e;
      }
    }
  }

  // --- Authorization ---
  const decision = authorize(policy, principal);
  if (!decision.ok) return authMessages.denied(decision.reason);

  // --- Dispatch ---
  try {
    const availableCommands = commandsAvailableToIntent(commands, intent, appContext);
    return await command.handler({
      options: intent.options ?? {},
      subcommand: intent.subcommand ?? null,
      principal,
      ctx: { ...appContext, commands: availableCommands },
      discordUserId: intent.discordUserId,
      discordHandle: intent.discordHandle,
    });
  } catch (err) {
    console.error(`Command ${intent.commandName} failed:`, err);
    return authMessages.internalError();
  }
}

// Autocomplete cannot be deferred and has a hard ~3s Discord budget, so the
// principal lookup must not be allowed to consume it. Resolve with `null` after
// this many ms; the resolver then runs promptly with an anonymous principal
// (typically yielding no suggestions) rather than the whole autocomplete timing
// out. Any directory call left in flight resolves harmlessly.
// NOTE: this timeout runs sequentially before command-specific autocomplete
// budgets (e.g. doc.js's AUTOCOMPLETE_TIMEOUT_MS). Their sum must stay under
// Discord's ~3s autocomplete window (doc-command.test.js enforces sum <= 2800).
// Raised 1000 -> 2000 to absorb Neon compute cold-start (~1.0-1.4s on an idle
// branch), which was pushing the principal lookup past 1000ms and resolving it
// to an anonymous principal (empty /team suggestions). Stopgap: the durable fix
// is keeping the directory DB warm (Neon autosuspend) or caching the lookup.
export const PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS = 2000;

// Resolve to `null` on timeout or rejection — never rejects.
function resolveWithTimeout(promise, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), ms);
    timer.unref?.();
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      () => {
        clearTimeout(timer);
        resolve(null);
      },
    );
  });
}

// Surface-agnostic autocomplete dispatch. Sibling to `dispatch`: takes a neutral
// autocomplete intent, resolves the focused option's resolver, and returns up to
// 25 { name, value } suggestions. BEST-EFFORT — it never throws to the surface.
// Any failure (unlinked caller, directory down, cold DB, resolver error) yields
// []. Autocomplete cannot be deferred, so a slow/failed lookup simply produces no
// suggestions and the user types the value manually.
export async function dispatchAutocomplete(intent, { commands, appContext }) {
  const command = commands.get(intent.commandName);
  if (!command) return [];
  const activeOptions = intent.subcommand
    ? (command.subcommands.find((s) => s.name === intent.subcommand)?.options ?? [])
    : command.options;
  const option = activeOptions.find((o) => o.name === intent.focusedOption);
  if (!option || typeof option.autocomplete !== 'function') return [];

  const principal = await resolveWithTimeout(
    resolvePrincipal(appContext.directory, intent.discordUserId),
    PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS,
  );

  try {
    const suggestions = await option.autocomplete({
      typed: intent.typed ?? '',
      principal,
      ctx: appContext,
    });
    return Array.isArray(suggestions) ? suggestions.slice(0, 25) : [];
  } catch {
    return [];
  }
}
