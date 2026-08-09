import { test } from 'node:test';
import assert from 'node:assert/strict';
import record from '../src/commands/record.js';
import { commands, partitionCommands } from '../src/commands/index.js';

test('record command is stable with start/status/stop subcommands', () => {
  assert.equal(record.name, 'record');
  assert.equal(record.beta, false);
  const names = record.subcommands.map((s) => s.name).sort();
  assert.deepEqual(names, ['start', 'status', 'stop']);
});

test('record is registered in the command registry', () => {
  assert.equal(commands.get('record'), record);
});

test('record is on the stable channel (registered globally, not testing-guild only)', () => {
  const { stable, beta } = partitionCommands([...commands.values()]);
  assert.ok(stable.some((c) => c.name === 'record'));
  assert.deepEqual(
    beta.map((c) => c.name),
    [],
  );
});

test('record handler is a guard that is never reached via the neutral path (adapter intercepts)', async () => {
  const payload = await record.handler({});
  assert.equal(payload.ephemeral, true);
  assert.match(payload.content, /voice surface/i);
});

test('record subcommand handlers are also guards', async () => {
  for (const sub of record.subcommands) {
    const payload = await sub.handler({});
    assert.equal(payload.ephemeral, true);
    assert.match(payload.content, /voice surface/i);
  }
});
