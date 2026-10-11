import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  jsonCommand,
  parsePreviewArgs,
  railwayApi,
  readPreviewSource,
} from '../scripts/lib/previewCli.js';

test('options requiring values never consume another flag', () => {
  for (const flag of ['--project', '--timeout-minutes']) {
    for (const trailing of [[], ['--plan'], ['--recordings-stopped']]) {
      assert.throws(() => parsePreviewArgs(['123', flag, ...trailing], {}), /Missing value/);
    }
  }
});

test('project precedence and dry-run mode survive valid arguments', () => {
  const options = parsePreviewArgs(
    ['123', '--project', 'override', '--plan', '--timeout-minutes', '60'],
    { MISTY_PREVIEW_PROJECT_ID: 'environment' },
  );
  assert.equal(options.projectId, 'override');
  assert.equal(options.planOnly, true);
  assert.equal(options.confirmedIdle, false);
  assert.equal(options.timeoutMs, 60 * 60_000);
  assert.equal(
    parsePreviewArgs(['123'], { MISTY_PREVIEW_PROJECT_ID: 'environment' }).projectId,
    'environment',
  );
});

test('deployment timeout options reject invalid durations', () => {
  for (const value of ['0', '-1', '1.5', 'oops', '9999999999999999999999']) {
    assert.throws(
      () => parsePreviewArgs(['123', '--timeout-minutes', value], {}),
      /--timeout-minutes/,
    );
  }
});

test('source metadata is fetched from the exact PR commit without executing PR code', async () => {
  const sha = 'a'.repeat(40);
  const result = await readPreviewSource(sha, async (command, args) => {
    assert.equal(command, 'gh');
    assert.deepEqual(args, ['api', `repos/UTMIST/Misty/git/trees/${sha}?recursive=1`]);
    return { stdout: '{"tree":[],"truncated":false}' };
  });
  assert.deepEqual(result, { tree: [], truncated: false });
});

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
  for (const stdout of ['{}', 'null', '{"data":null}', '{"data":{}}']) {
    await assert.rejects(
      railwayApi('query', {}, async () => ({ stdout })),
      { message: 'Railway API returned no data.' },
    );
  }
});

test('null resources identify the missing field and requested ID', async () => {
  for (const field of ['project', 'environment', 'deployment', 'deployments', 'projectToken']) {
    await assert.rejects(
      railwayApi('query', { id: 'missing-id' }, async () => ({
        stdout: JSON.stringify({ data: { [field]: null } }),
      })),
      new RegExp(`no ${field} for missing-id.*deleted or access changed`),
    );
  }
});
