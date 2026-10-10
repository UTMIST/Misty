import { test } from 'node:test';
import assert from 'node:assert/strict';
import { commands } from '../src/commands/index.js';
import {
  flattenCommands,
  collectOptions,
  matchesSearch,
  categories,
} from '../src/web/public/catalog.js';

test('the web catalog preserves every registry command, subcommand, option, and policy', () => {
  const raw = [...commands.values()];
  const flat = flattenCommands(raw);
  assert.equal(
    flat.length,
    raw.reduce((count, command) => count + (command.subcommands.length || 1), 0),
  );
  assert.equal(new Set(flat.map((command) => command.key)).size, flat.length);
  for (const entry of flat) {
    const parent = commands.get(entry.parentName);
    const original = entry.subName
      ? parent.subcommands.find((sub) => sub.name === entry.subName)
      : parent;
    assert.deepEqual(entry.options, original.options);
    assert.equal(entry.auth, original.auth);
    assert.ok(categories.some((category) => category.id === entry.category));
  }
  assert.equal(flat.find((entry) => entry.key === 'doc:remove').auth, 'admin');
  assert.equal(flat.find((entry) => entry.key === 'doc:list').auth, 'linked');
});

test('future commands remain discoverable without a presentation mapping', () => {
  const [entry] = flattenCommands([
    {
      name: 'new-feature',
      description: 'Find something useful',
      auth: 'linked',
      options: [],
      subcommands: [],
    },
  ]);
  assert.equal(entry.category, 'other');
  assert.ok(matchesSearch(entry, 'OTHER useful'));
  assert.ok(matchesSearch(entry, 'new-feature'));
  assert.ok(!matchesSearch(entry, 'other missing'));
});

test('search finds friendly labels, slash commands, and categories', () => {
  const flat = flattenCommands([...commands.values()]);
  assert.ok(
    matchesSearch(
      flat.find((entry) => entry.key === 'whoami'),
      'profile',
    ),
  );
  assert.ok(
    matchesSearch(
      flat.find((entry) => entry.key === 'team:add'),
      'team add',
    ),
  );
  assert.ok(
    matchesSearch(
      flat.find((entry) => entry.key === 'doc:show'),
      'documents',
    ),
  );
});

test('optional defaults stay omitted while explicit false, zero, and user IDs survive', () => {
  const definitions = [
    { name: 'user', type: 'user', required: true },
    { name: 'role', type: 'string', required: false },
    { name: 'active_only', type: 'boolean', required: false },
    { name: 'limit', type: 'string', required: false },
  ];
  const payload = collectOptions(
    [
      ['user', '100000000000000002'],
      ['role', ''],
      ['active_only', 'false'],
      ['limit', '0'],
      ['unregistered', 'ignored'],
    ],
    definitions,
  );
  assert.deepEqual(payload, { user: '100000000000000002', active_only: 'false', limit: '0' });
  assert.deepEqual(collectOptions([['active_only', '']], definitions), {});
});
