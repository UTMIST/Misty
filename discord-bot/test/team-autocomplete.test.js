import { test } from 'node:test';
import assert from 'node:assert/strict';
import team from '../src/commands/team.js';
import { allTeamsAutocomplete } from '../src/commands/teamAutocomplete.js';

function findSub(name) {
  return team.subcommands.find((s) => s.name === name);
}

function teamOptOf(subName) {
  const sub = findSub(subName);
  // rename targets its team via `slug`; add/remove/roster via `team`.
  return sub.options.find((o) => o.name === 'team' || o.name === 'slug');
}

test('admin subcommands that target a team have an autocomplete resolver', () => {
  for (const name of ['rename', 'add', 'remove', 'roster']) {
    const opt = teamOptOf(name);
    assert.equal(
      typeof opt.autocomplete,
      'function',
      `${name} team/slug option should autocomplete`,
    );
  }
});

test("allTeamsAutocomplete suggests ALL active teams, not just the caller's", async () => {
  const ctx = {
    directory: {
      // If this resolver ever calls listMemberships it is wrongly scoping to the
      // caller — admin commands act on any team, so fail loudly.
      listMemberships: async () => {
        throw new Error('should not scope to memberships');
      },
      listTeams: async ({ activeOnly }) => {
        assert.equal(activeOnly, true);
        return [
          { id: 't1', slug: 'ml', label: 'Machine Learning' },
          { id: 't2', slug: 'ops', label: 'Operations' },
        ];
      },
    },
  };
  const out = await allTeamsAutocomplete({ typed: '', principal: { person: { id: 'p1' } }, ctx });
  assert.deepEqual(out, [
    { name: 'Machine Learning', value: 'ml' },
    { name: 'Operations', value: 'ops' },
  ]);
});

test('allTeamsAutocomplete filters by typed against label and slug', async () => {
  const ctx = {
    directory: {
      listTeams: async () => [
        { id: 't1', slug: 'ml', label: 'Machine Learning' },
        { id: 't2', slug: 'ops', label: 'Operations' },
      ],
    },
  };
  const byLabel = await allTeamsAutocomplete({
    typed: 'oper',
    principal: { person: { id: 'p1' } },
    ctx,
  });
  assert.deepEqual(byLabel, [{ name: 'Operations', value: 'ops' }]);
  const bySlug = await allTeamsAutocomplete({
    typed: 'ml',
    principal: { person: { id: 'p1' } },
    ctx,
  });
  assert.deepEqual(bySlug, [{ name: 'Machine Learning', value: 'ml' }]);
});

test('allTeamsAutocomplete returns [] when principal is null', async () => {
  const out = await allTeamsAutocomplete({ typed: '', principal: null, ctx: { directory: {} } });
  assert.deepEqual(out, []);
});

test('allTeamsAutocomplete drops teams with no usable label', async () => {
  const ctx = {
    directory: {
      listTeams: async () => [
        { id: 't1', slug: 'ml', label: 'Machine Learning' },
        { id: 't2', slug: 'ghost', label: null },
      ],
    },
  };
  const out = await allTeamsAutocomplete({ typed: '', principal: { person: { id: 'p1' } }, ctx });
  assert.deepEqual(out, [{ name: 'Machine Learning', value: 'ml' }]);
});

test('allTeamsAutocomplete returns [] when the lookup exceeds the budget', async () => {
  // Lookup settles, but only after the tiny budget elapses, so the timeout wins
  // the race and the resolver yields []. It still SETTLES so no pending promise
  // is left for the runner to flag.
  const slow = () => new Promise((r) => setTimeout(() => r([]), 80));
  const ctx = {
    _autocompleteTimeoutMs: 10,
    directory: { listTeams: slow },
  };
  const out = await allTeamsAutocomplete({ typed: '', principal: { person: { id: 'p1' } }, ctx });
  assert.deepEqual(out, []);
});
