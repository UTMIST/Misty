const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

// Execute the actual github-script body with runner/API boundaries replaced.
// This catches activation failures without exposing a token or updating a board.
const workflow = readFileSync(join(__dirname, '../workflows/project-board.yml'), 'utf8');
const marker = '          script: |\n';
assert.ok(workflow.includes(marker));
const script = workflow.slice(workflow.indexOf(marker) + marker.length).replace(/^ {12}/gm, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('require', 'process', 'context', 'core', 'github', script);

function runner({ event = 'pull_request_review', installed = false, workflowPresent = false,
  token = '', dryRun = 'false' } = {}) {
  const calls = [];
  const notices = [];
  const summaries = [];
  const core = {
    notice: (message) => notices.push(message),
    info: () => {},
    summary: {
      addHeading(value) { summaries.push(value); return this; },
      addRaw(value) { summaries.push(value); return this; },
      async write() {},
    },
  };
  const load = (name) => {
    if (name === 'node:fs') return { existsSync(path) {
      if (path === './.github/scripts/project-board.cjs') return installed;
      if (path === './.github/workflows/project-board.yml') return workflowPresent;
      throw new Error(`Unexpected installation path: ${path}`);
    } };
    assert.equal(name, './.github/scripts/project-board.cjs');
    if (!installed) throw new Error('MODULE_NOT_FOUND: project-board.cjs');
    return { reconcile: async (args) => {
      calls.push(args);
      return { dryRun: args.dryRun, issues: 0, pullRequests: 0, changes: [] };
    } };
  };
  return {
    calls, notices, summaries,
    run: () => run(load, { env: { PROJECTS_TOKEN: token, DRY_RUN: dryRun } },
      { eventName: event, repo: { owner: 'UTMIST', repo: 'Misty' } }, core, {}),
  };
}

for (const token of ['', 'offline-fake']) {
  test(`a review before installation skips reconciliation with token configured: ${Boolean(token)}`, async () => {
    const f = runner({ token });
    await f.run();
    assert.equal(f.calls.length, 0);
    assert.match(f.notices.join('\n'), /not installed/);
    assert.match(f.summaries.join('\n'), /Skipping this review event/);
  });
}

test('installed review and manual runs still reconcile and preserve dry-run selection', async () => {
  for (const [event, dryRun] of [['pull_request_review', 'false'], ['workflow_dispatch', 'true']]) {
    const f = runner({ event, dryRun, installed: true, workflowPresent: true, token: 'offline-fake' });
    await f.run();
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].dryRun, dryRun === 'true');
    assert.deepEqual(f.notices, []);
  }
});

test('a missing installed script and non-review activation failures remain visible', async () => {
  for (const config of [
    { workflowPresent: true },
    { event: 'schedule' },
    { event: 'workflow_dispatch' },
    { event: 'pull_request_target' },
    { event: 'issues' },
  ]) {
    const f = runner({ ...config, token: 'offline-fake' });
    await assert.rejects(f.run(), /MODULE_NOT_FOUND/);
    assert.equal(f.calls.length, 0);
    assert.deepEqual(f.notices, []);
  }
});

test('installed automation still fails on missing project credentials', async () => {
  const f = runner({ installed: true, workflowPresent: true });
  await assert.rejects(f.run(), /PROJECTS_TOKEN is required/);
  assert.equal(f.calls.length, 0);
});
