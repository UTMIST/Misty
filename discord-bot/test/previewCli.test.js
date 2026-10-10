import { test } from 'node:test';
import assert from 'node:assert/strict';
import { jsonCommand, railwayApi } from '../scripts/lib/previewCli.js';

test('a missing CLI is identified by name with an installation hint', async () => {
  for (const command of ['gh', 'railway']) {
    await assert.rejects(
      jsonCommand(command, [], async () => {
        throw Object.assign(new Error('spawn ENOENT'), { code: 'ENOENT' });
      }),
      new RegExp(`${command} failed: executable not found on PATH; install`),
    );
  }
});

test('nonzero CLI exits retain distinct login, access, and API diagnostics', async () => {
  for (const stderr of [
    'Please login with railway login',
    'Not Authorized',
    'GraphQL request failed',
  ]) {
    await assert.rejects(
      jsonCommand('railway', [], async () => {
        throw Object.assign(new Error('Command failed'), { code: 1, stderr: `${stderr}\n` });
      }),
      { message: `railway failed: ${stderr}` },
    );
  }
});

test('errors without stderr retain their cause, and timeouts are identified', async () => {
  await assert.rejects(
    jsonCommand('gh', [], async () => {
      throw new Error('Permission denied');
    }),
    /gh failed: Permission denied/,
  );
  await assert.rejects(
    jsonCommand('railway', [], async () => {
      throw Object.assign(new Error('Command failed'), { killed: true, signal: 'SIGTERM' });
    }),
    /railway failed: timed out after 60 seconds/,
  );
});

test('invalid JSON is distinguished from command execution failure', async () => {
  await assert.rejects(
    jsonCommand('gh', [], async () => ({ stdout: 'not JSON' })),
    { message: 'gh returned invalid JSON.' },
  );
});

test('Railway GraphQL errors are requested as JSON and report each message', async () => {
  await assert.rejects(
    railwayApi(
      'query PreviewProject($id: String!) { project(id: $id) { id } }',
      { id: 'project' },
      async (command, args) => {
        assert.equal(command, 'railway');
        assert.ok(args.includes('--allow-errors'));
        assert.deepEqual(JSON.parse(args[args.indexOf('--variables') + 1]), { id: 'project' });
        return {
          stdout: JSON.stringify({
            data: {},
            errors: [{ message: 'Not Authorized' }, { message: 'Project unavailable' }],
          }),
        };
      },
    ),
    { message: 'Railway API: Not Authorized; Project unavailable' },
  );
});

test('Railway data is returned only when present and error-free', async () => {
  assert.deepEqual(
    await railwayApi('query', {}, async () => ({
      stdout: '{"data":{"project":{"id":"project"}}}',
    })),
    { project: { id: 'project' } },
  );
  await assert.rejects(
    railwayApi('query', {}, async () => ({ stdout: '{}' })),
    { message: 'Railway API returned no data.' },
  );
});
