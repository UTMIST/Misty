const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, mkdirSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { test } = require('node:test');
const { prepareSync, sync, openPromotion } = require('./promotion.cjs');

const repo = 'UTMIST/Misty';
const root = `repos/${repo}`;
const quiet = () => {};

test('the credentialed entry point rejects other refs, repositories and event types', () => {
  for (const change of [
    { GITHUB_REF: 'refs/heads/staging' },
    { GITHUB_REF: 'refs/tags/main' },
    { GITHUB_REPOSITORY: 'someone/Misty' },
    { GITHUB_EVENT_NAME: 'pull_request' },
  ]) {
    assert.throws(() => execFileSync(process.execPath, [join(__dirname, 'promotion.cjs'), 'sync'], {
      env: { ...process.env, GITHUB_REF: 'refs/heads/main', GITHUB_REPOSITORY: repo,
        GITHUB_EVENT_NAME: 'workflow_dispatch', ...change },
      stdio: ['pipe', 'pipe', 'pipe'],
    }), (error) => /Run promotion or sync from main/.test(error.stderr.toString()));
  }
});

// Real local Git graphs exercise release ancestry and concurrent staging writes.
// GitHub is faked; these tests never use a token or make a network request.
function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'misty-promotion-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const remote = join(dir, 'remote.git');
  const work = join(dir, 'work');
  mkdirSync(work);
  const git = (...args) => execFileSync('git', args, {
    cwd: work, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'],
  }).trim();
  git('init', '--bare', remote);
  git('init', '-b', 'main');
  git('config', 'user.name', 'Release test');
  git('config', 'user.email', 'release-test@example.invalid');
  git('config', 'commit.gpgsign', 'false');
  git('config', 'core.hooksPath', '/dev/null');
  const commit = (file, content) => {
    writeFileSync(join(work, file), content);
    git('add', file);
    git('commit', '-m', `Update ${file}`);
    return git('rev-parse', 'HEAD');
  };
  commit('release.txt', 'old\n');
  git('checkout', '-b', 'staging');
  commit('release.txt', 'released\n');
  git('checkout', 'main');
  git('merge', '--no-ff', 'staging', '-m', 'Production promotion');
  const main = git('rev-parse', 'HEAD');
  git('checkout', 'staging');
  git('remote', 'add', 'origin', remote);
  git('push', 'origin', 'main', 'staging');
  const tree = (ref) => git('rev-parse', `${ref}^{tree}`);
  return { git, commit, main, tree, work };
}

function fakeGithub(f, overrides = {}) {
  let pr;
  let creates = 0;
  let merges = 0;
  let polls = 0;
  const request = (method, path, body) => {
    if (method === 'GET' && path.startsWith(`${root}/pulls?`)) return pr ? [pr] : [];
    if (method === 'POST' && path === `${root}/pulls`) {
      creates++;
      const sha = f.git('ls-remote', 'origin', `refs/heads/${body.head}`).split(/\s/)[0];
      pr = { number: 1, html_url: 'https://github.com/UTMIST/Misty/pull/1',
        state: 'open', merged: false, draft: false, mergeable: true,
        head: { sha, ref: body.head, repo: { full_name: repo } },
        base: { ref: body.base, repo: { full_name: repo } } };
      overrides.created?.(pr);
      return pr;
    }
    if (method === 'GET' && path === `${root}/pulls/1`) return pr;
    if (method === 'GET' && path.startsWith(`${root}/actions/workflows/ci.yml/runs?`)) {
      polls++;
      const run = { id: 1, status: 'completed', conclusion: 'success',
        head_sha: pr.head.sha, head_branch: pr.head.ref,
        head_repository: { full_name: repo }, html_url: 'https://github.com/ci/1' };
      return { workflow_runs: overrides.runs ? overrides.runs(run, polls) : [run] };
    }
    if (method === 'PUT' && path === `${root}/pulls/1/merge`) {
      merges++;
      assert.equal(body.sha, pr.head.sha);
      assert.equal(body.merge_method, 'merge');
      overrides.beforeMerge?.();
      f.git('checkout', 'staging');
      f.git('merge', '--no-ff', body.sha, '-m', 'Merge sync PR');
      f.git('push', 'origin', 'staging');
      pr.merged = true;
      pr.state = 'closed';
      return { merged: true };
    }
    throw new Error(`Unexpected API request: ${method} ${path}`);
  };
  return { request, counts: () => ({ creates, merges, polls }) };
}

function options(f, gh, extra = {}) {
  return { repo, runGit: f.git, request: gh.request, report: quiet,
    wait: async () => {}, attempts: 3, ...extra };
}

test('sync preserves staging files and release ancestry, then is a no-op on retry', async (t) => {
  const f = fixture(t);
  f.commit('feature.txt', 'new staging work\n');
  f.git('push', 'origin', 'staging');
  const before = f.tree('staging');
  const gh = fakeGithub(f);
  await sync(options(f, gh));
  assert.equal(f.tree('staging'), before);
  f.git('merge-base', '--is-ancestor', f.main, 'staging');
  await sync(options(f, gh));
  assert.deepEqual(gh.counts(), { creates: 1, merges: 1, polls: 1 });
});

test('staging can advance even during the final merge without losing its changes', async (t) => {
  const f = fixture(t);
  let before;
  const gh = fakeGithub(f, { beforeMerge() {
    // Revert a released file: a direct main→staging merge could reapply it.
    f.commit('release.txt', 'old\n');
    f.git('push', 'origin', 'staging');
    before = f.tree('staging');
  } });
  await sync(options(f, gh));
  assert.equal(f.tree('staging'), before);
  f.git('merge-base', '--is-ancestor', f.main, 'staging');
});

test('clean main-only content changes and conflicts cannot create a sync branch', (t) => {
  for (const conflict of [false, true]) {
    const f = fixture(t);
    f.git('checkout', 'main');
    f.commit('release.txt', 'production hotfix\n');
    f.git('push', 'origin', 'main');
    f.git('checkout', 'staging');
    if (conflict) {
      f.commit('release.txt', 'different staging fix\n');
      f.git('push', 'origin', 'staging');
    }
    const before = f.tree('staging');
    assert.throws(() => prepareSync(f.git));
    assert.equal(f.tree('staging'), before);
    assert.equal(f.git('ls-remote', '--heads', 'origin', 'refs/heads/automation/*'), '');
  }
});

test('retry reuses an existing validated sync snapshot and open PR', async (t) => {
  const f = fixture(t);
  const snapshot = prepareSync(f.git);
  const gh = fakeGithub(f, { runs: (run, poll) => poll === 1 ? [] : [run] });
  await assert.rejects(sync(options(f, gh, { attempts: 1 })), /Timed out/);
  assert.equal(prepareSync(f.git).sha, snapshot.sha);
  await sync(options(f, gh));
  assert.deepEqual(gh.counts(), { creates: 1, merges: 1, polls: 2 });
});

test('an existing branch with changed files is rejected without force-pushing', (t) => {
  const f = fixture(t);
  const bad = f.commit('unexpected.txt', 'do not auto-merge\n');
  const ref = `refs/heads/automation/sync-main-${f.main}`;
  f.git('push', 'origin', `${bad}:${ref}`);
  assert.throws(() => prepareSync(f.git), /not an unchanged staging snapshot/);
  assert.equal(f.git('ls-remote', 'origin', ref).split(/\s/)[0], bad);
});

test('a snapshot becomes invalid if staging history has been rewritten', (t) => {
  const f = fixture(t);
  f.commit('feature.txt', 'feature\n');
  f.git('push', 'origin', 'staging');
  prepareSync(f.git);
  f.git('reset', '--hard', 'staging~1');
  // Only this disposable fixture intentionally force-pushes to model bad history.
  f.git('push', '--force', 'origin', 'staging');
  assert.throws(() => prepareSync(f.git), /not an unchanged staging snapshot/);
});

test('failed, cancelled, missing, or wrong-head CI never allows a merge', async (t) => {
  for (const conclusion of ['failure', 'cancelled', 'missing', 'wrong-head']) {
    const f = fixture(t);
    const gh = fakeGithub(f, { runs(run) {
      if (conclusion === 'missing') return [];
      if (conclusion === 'wrong-head') return [{ ...run, head_sha: 'f'.repeat(40) }];
      return [{ ...run, conclusion }];
    } });
    await assert.rejects(sync(options(f, gh, { attempts: 1 })), /did not pass|Timed out/);
    assert.equal(gh.counts().merges, 0);
  }
});

test('a pending rerun overrides an earlier successful CI run', async (t) => {
  const f = fixture(t);
  const gh = fakeGithub(f, { runs(run, poll) {
    return [run, { ...run, id: 2, status: poll < 2 ? 'in_progress' : 'completed',
      conclusion: poll < 2 ? null : 'success' }];
  } });
  await sync(options(f, gh));
  assert.equal(gh.counts().polls, 2);
  assert.equal(gh.counts().merges, 1);
});

test('a changed head, wrong target, draft, or closed PR requires intervention', async (t) => {
  for (const change of [
    (pr) => { pr.head.sha = 'f'.repeat(40); },
    (pr) => { pr.base.ref = 'main'; },
    (pr) => { pr.head.repo.full_name = 'someone/else'; },
    (pr) => { pr.draft = true; },
    (pr) => { pr.state = 'closed'; },
  ]) {
    const f = fixture(t);
    const gh = fakeGithub(f, { created: change });
    await assert.rejects(sync(options(f, gh)), /refusing to merge|closed or made draft/);
    assert.equal(gh.counts().merges, 0);
  }
});

test('the server can refuse a merge without falling back to an override or direct push', async (t) => {
  const f = fixture(t);
  const gh = fakeGithub(f);
  const before = f.git('rev-parse', 'staging');
  const request = (method, path, body) => {
    if (method === 'PUT') throw new Error('Required status check is expected');
    return gh.request(method, path, body);
  };
  await assert.rejects(sync(options(f, gh, { request })), /Required status/);
  assert.equal(f.git('rev-parse', 'staging'), before);
});

test('promotion sync must finish first, and an unchanged tree needs no release PR', async (t) => {
  const f = fixture(t);
  const gh = fakeGithub(f);
  assert.throws(() => openPromotion(options(f, gh)), /sync it first/);
  await sync(options(f, gh));
  const request = (method) => { assert.equal(method, 'GET'); return []; };
  assert.equal(openPromotion(options(f, gh, { request })), undefined);
});

test('promotion opens staging→main once and preserves an existing release description', async (t) => {
  const f = fixture(t);
  const gh = fakeGithub(f);
  await sync(options(f, gh));
  f.commit('feature.txt', 'next release\n');
  f.git('push', 'origin', 'staging');
  let existing;
  let creates = 0;
  const request = (method, path, body) => {
    if (method === 'GET') return existing ? [existing] : [];
    assert.equal(method, 'POST');
    assert.equal(body.base, 'main');
    assert.equal(body.head, 'staging');
    creates++;
    existing = { html_url: 'https://github.com/UTMIST/Misty/pull/2', ...body };
    return existing;
  };
  openPromotion(options(f, gh, { request }));
  existing.body = 'Maintainer release notes';
  openPromotion(options(f, gh, { request }));
  assert.equal(creates, 1);
  assert.equal(existing.body, 'Maintainer release notes');
});
