import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSnapshotCommands } from '../scripts/lib/snapshotDb.js';

test('buildSnapshotCommands emits the drop, create, and pg_dump-piped-into-psql sequence', () => {
  const cmds = buildSnapshotCommands({
    source: 'team_tracking',
    target: 'team_tracking_playground',
    dbUser: 'team_tracking',
  });
  // Each entry is a shell-safe array of tokens.
  assert.equal(cmds.length, 3);
  // Drop
  assert.deepEqual(cmds[0], [
    'docker',
    'compose',
    'exec',
    '-T',
    'postgres',
    'psql',
    '-U',
    'team_tracking',
    '-d',
    'postgres',
    '-c',
    'DROP DATABASE IF EXISTS team_tracking_playground;',
  ]);
  // Create
  assert.deepEqual(cmds[1], [
    'docker',
    'compose',
    'exec',
    '-T',
    'postgres',
    'psql',
    '-U',
    'team_tracking',
    '-d',
    'postgres',
    '-c',
    'CREATE DATABASE team_tracking_playground;',
  ]);
  // Dump-and-restore pipeline described as a single sh -c line
  assert.equal(cmds[2][0], 'sh');
  assert.equal(cmds[2][1], '-c');
  assert.ok(cmds[2][2].includes('pg_dump'));
  assert.ok(cmds[2][2].includes('team_tracking_playground'));
  // pipefail is load-bearing: without it, a pg_dump failure is swallowed by
  // psql exiting 0 on an empty stream, leaving the scratch DB empty.
  assert.ok(cmds[2][2].includes('pipefail'));
});
