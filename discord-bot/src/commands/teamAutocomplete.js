// Shared team-slug autocomplete resolvers for slash-command `team`/`slug` options.
//
// Autocomplete cannot be deferred and must answer within Discord's ~3s window, so
// every resolver here bounds its directory lookups against a budget and degrades
// to no suggestions (rather than hanging) when the directory is cold/slow — the
// field still accepts a typed slug.
//
// This budget is spent AFTER router.js's PRINCIPAL_AUTOCOMPLETE_TIMEOUT_MS (2000ms)
// has already resolved the principal, so 2000 + 700 = 2700ms worst case stays
// under the window. The principal lookup wakes the (possibly cold) directory DB,
// so by the time this budget runs the same DB is warm (~80ms) and 700ms is ample.
export const AUTOCOMPLETE_TIMEOUT_MS = 700;

// Race a directory lookup against the autocomplete budget. Resolves to the lookup
// result, or null if the budget elapses first. The timeout promise still SETTLES
// (resolve(null)) rather than hanging, so no pending promise is left behind for a
// test runner to flag as "Promise resolution is still pending".
function withinBudget(ctx, lookup) {
  const budget = ctx._autocompleteTimeoutMs ?? AUTOCOMPLETE_TIMEOUT_MS;
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve(null), budget);
    timer.unref?.();
  });
  return Promise.race([lookup, timeout]).finally(() => clearTimeout(timer));
}

// Filter a team list by the typed needle (matching label or slug) and map to
// Discord choices. A team with no usable label can't be shown as a suggestion —
// drop it instead of emitting a { name: null } choice Discord would reject.
function toChoices(teams, typed) {
  const needle = (typed ?? '').toLowerCase();
  return teams
    .filter((t) => typeof t.label === 'string' && t.label.length > 0)
    .filter(
      (t) =>
        t.label.toLowerCase().includes(needle) || (t.slug ?? '').toLowerCase().includes(needle),
    )
    .slice(0, 25)
    .map((t) => ({ name: t.label, value: t.slug }));
}

// Suggest the caller's own active teams. Used by /doc's `team` option, where the
// natural scope is "teams I'm on".
export async function myTeamsAutocomplete({ typed, principal, ctx }) {
  if (!principal?.person?.id) return [];
  // Two directory calls (memberships + all teams) instead of N per-team lookups.
  const result = await withinBudget(
    ctx,
    Promise.all([
      ctx.directory.listMemberships({ personId: principal.person.id, activeOnly: true }),
      ctx.directory.listTeams({ activeOnly: true }),
    ]),
  );
  if (result === null) return []; // timed out — degrade to no suggestions
  const [memberships, teams] = result;
  const myTeamIds = new Set(memberships.map((m) => m.team_id));
  return toChoices(
    teams.filter((t) => myTeamIds.has(t.id)),
    typed,
  );
}

// Suggest ALL active teams. Used by /team's admin subcommands, which operate on
// any team — scoping to the caller's memberships would wrongly hide teams they
// aren't on. The full active-team list is already visible via `/team list`
// (auth: linked), so this discloses nothing new.
export async function allTeamsAutocomplete({ typed, principal, ctx }) {
  if (!principal?.person?.id) return [];
  const result = await withinBudget(ctx, ctx.directory.listTeams({ activeOnly: true }));
  if (result === null) return []; // timed out — degrade to no suggestions
  return toChoices(result, typed);
}
