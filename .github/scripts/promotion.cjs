// Release orchestration uses only Node built-ins, git and gh. Never load staging
// code into the credentialed runner; merge-tree/commit-tree operate on objects.
const { execFileSync } = require('node:child_process');
const { appendFileSync } = require('node:fs');
const { setTimeout: sleep } = require('node:timers/promises');

function command(binary, args, options = {}) {
  return execFileSync(binary, args, {
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'],
    timeout: 120_000,
    ...options,
  }).trim();
}

function git(...args) {
  return command('git', args);
}

function api(method, path, body) {
  const args = ['api', '--method', method, path];
  if (body !== undefined) args.push('--input', '-');
  return JSON.parse(command('gh', args, {
    input: body === undefined ? undefined : JSON.stringify(body),
  }));
}

function fetchBranches(runGit) {
  runGit('fetch', '--no-tags', 'origin',
    '+refs/heads/main:refs/remotes/origin/main',
    '+refs/heads/staging:refs/remotes/origin/staging');
  return {
    main: runGit('rev-parse', 'refs/remotes/origin/main'),
    staging: runGit('rev-parse', 'refs/remotes/origin/staging'),
  };
}

function isAncestor(runGit, ancestor, descendant) {
  try {
    runGit('merge-base', '--is-ancestor', ancestor, descendant);
    return true;
  } catch (error) {
    if (error.status === 1) return false;
    throw error;
  }
}

function tree(runGit, sha) {
  return runGit('rev-parse', `${sha}^{tree}`);
}

function assertSnapshot(runGit, sha, main, staging) {
  const parents = runGit('show', '-s', '--format=%P', sha).split(' ');
  if (parents.length !== 2 || parents[1] !== main ||
      !isAncestor(runGit, parents[0], staging) ||
      tree(runGit, sha) !== tree(runGit, parents[0])) {
    throw new Error('Sync branch is not an unchanged staging snapshot merging this main commit.');
  }
  // Parent identities and tree equality alone can conceal a discarded hotfix
  // or an older staging snapshot. Recompute the merge, including on retries.
  const mergedTree = runGit('merge-tree', '--write-tree', parents[0], main).split('\n')[0];
  if (mergedTree !== tree(runGit, sha)) {
    throw new Error('Sync snapshot does not match its parents\' merge result.');
  }
  const currentTree = runGit('merge-tree', '--write-tree', staging, sha).split('\n')[0];
  if (currentTree !== tree(runGit, staging)) {
    throw new Error('Sync snapshot would change current staging files.');
  }
}

function prepareSync(runGit = git) {
  const { main, staging } = fetchBranches(runGit);
  if (isAncestor(runGit, main, staging)) return null;

  const branch = `automation/sync-main-${main}`;
  const ref = `refs/heads/${branch}`;
  const existing = runGit('ls-remote', '--heads', 'origin', ref);
  let sha;
  if (existing) {
    runGit('fetch', '--no-tags', 'origin', ref);
    sha = runGit('rev-parse', 'FETCH_HEAD');
    assertSnapshot(runGit, sha, main, staging);
  } else {
    // A conflict exits nonzero. Even a clean merge must leave every file intact.
    const mergedTree = runGit('merge-tree', '--write-tree', staging, main).split('\n')[0];
    if (mergedTree !== tree(runGit, staging)) {
      throw new Error('Sync would change staging files. Reconcile main through a reviewed PR.');
    }
    sha = runGit('commit-tree', mergedTree, '-p', staging, '-p', main,
      '-m', `chore: sync main ${main.slice(0, 12)} into staging`);
    assertSnapshot(runGit, sha, main, staging);
    // Only create the automation branch. Never push directly to main/staging,
    // and never overwrite an existing branch after a concurrent update.
    runGit('push', 'origin', `${sha}:${ref}`);
  }
  return { main, staging, branch, sha };
}

async function sync({ repo, runGit = git, request = api, wait = sleep,
  attempts = 60, report = console.log }) {
  const snapshot = prepareSync(runGit);
  if (!snapshot) {
    report('Staging already contains main; no sync is needed.');
    return;
  }
  const { main, branch, sha } = snapshot;
  const root = `repos/${repo}`;
  const head = `${repo.split('/')[0]}:${branch}`;
  const matches = request('GET', `${root}/pulls?state=all&base=staging&head=${encodeURIComponent(head)}`);
  let pr = matches[0];
  if (!pr) {
    pr = request('POST', `${root}/pulls`, {
      base: 'staging', head: branch,
      title: `chore: sync main ${main.slice(0, 12)} back into staging`,
      body: `Record production release ancestry in staging.\n\n` +
        `Main: \`${main}\`\nSnapshot: \`${sha}\`\n\n` +
        `The sync commit preserves its staging parent's exact tree. ` +
        `No files change. The release bot waits for CI, then merges this PR ` +
        `with a merge commit to preserve ancestry. It never force-pushes.\n\n` +
        `Runbook: [production promotion](https://github.com/${repo}/blob/main/docs/DEVELOPMENT.md#production-promotion).`,
    });
  }
  report(`Sync PR: ${pr.html_url}`);

  for (let attempt = 0; attempt < attempts; attempt++) {
    const current = request('GET', `${root}/pulls/${pr.number}`);
    if (current.head.sha !== sha || current.head.ref !== branch ||
        current.head.repo?.full_name !== repo || current.base.ref !== 'staging' ||
        current.base.repo?.full_name !== repo) {
      throw new Error('Sync PR head or target changed; refusing to merge.');
    }
    if (current.merged) {
      const latest = fetchBranches(runGit);
      if (!isAncestor(runGit, main, latest.staging)) {
        throw new Error('Merged sync PR did not preserve main ancestry.');
      }
      report(`Staging now contains main ${main}.`);
      return;
    }
    if (current.state !== 'open' || current.draft) {
      throw new Error('Sync PR was closed or made draft; restore it before retrying.');
    }

    const runs = request('GET', `${root}/actions/workflows/ci.yml/runs?event=pull_request&head_sha=${sha}&per_page=100`)
      .workflow_runs.filter((run) => run.head_sha === sha && run.head_branch === branch &&
        run.head_repository?.full_name === repo)
      .sort((a, b) => b.id - a.id);
    const ci = runs[0];
    if (ci?.status === 'completed' && ci.conclusion !== 'success') {
      throw new Error(`Sync CI did not pass: ${ci.html_url}. Fix or rerun CI, then retry sync.`);
    }
    if (ci?.conclusion === 'success' && ci.status === 'completed' && current.mergeable === true) {
      // A snapshot PR has no file delta from its staging parent, so subsequent
      // staging commits remain intact even if staging advances during this call.
      // The exact head is pinned; required status checks remain server-enforced.
      const latest = fetchBranches(runGit);
      assertSnapshot(runGit, sha, main, latest.staging);
      const result = request('PUT', `${root}/pulls/${pr.number}/merge`, {
        sha, merge_method: 'merge',
      });
      if (!result.merged) throw new Error(`Sync merge refused: ${result.message}`);
      const after = fetchBranches(runGit);
      if (!isAncestor(runGit, main, after.staging)) {
        throw new Error('Sync merge returned success but main is not in staging ancestry.');
      }
      report(`Merged ${pr.html_url}; staging contains main ${main}.`);
      return;
    }
    if (current.mergeable === false) throw new Error('Sync PR has conflicts; manual reconciliation is required.');
    if (attempt + 1 < attempts) await wait(30_000);
  }
  throw new Error(`Timed out waiting for sync CI. Rerun sync after checking ${pr.html_url}.`);
}

function openPromotion({ repo, runGit = git, request = api, report = console.log }) {
  const { main, staging } = fetchBranches(runGit);
  if (!isAncestor(runGit, main, staging)) {
    throw new Error('Main advanced during promotion preparation. Rerun promotion to sync it first.');
  }
  const root = `repos/${repo}`;
  const head = encodeURIComponent(`${repo.split('/')[0]}:staging`);
  const existing = request('GET', `${root}/pulls?state=open&base=main&head=${head}`)[0];
  if (existing) {
    report(`Promotion PR already open: ${existing.html_url}`);
    return existing;
  }
  if (tree(runGit, main) === tree(runGit, staging)) {
    report('No file changes to promote.');
    return;
  }
  const pr = request('POST', `${root}/pulls`, {
    base: 'main', head: 'staging', title: 'Release: promote staging to production',
    body: `Promote the reviewed and validated staging changes to production.\n\n` +
      `Candidate when opened: \`${staging}\`\nProduction base: \`${main}\`\n\n` +
      `- [ ] Review the current release diff and deployment requirements.\n` +
      `- [ ] Confirm staging validation and required PR checks.\n\n` +
      `Merge with a **merge commit** after normal approval. Merging deploys production ` +
      `and starts the automatic history sync back to staging. Staging remains a live ` +
      `branch: additional staging merges update this PR and must be included in review.`,
  });
  report(`Promotion PR: ${pr.html_url}`);
  return pr;
}

async function main() {
  const mode = process.argv[2];
  if (process.env.GITHUB_REPOSITORY !== 'UTMIST/Misty' ||
      process.env.GITHUB_REF !== 'refs/heads/main' ||
      !['promote', 'sync'].includes(mode) ||
      !['push', 'workflow_dispatch'].includes(process.env.GITHUB_EVENT_NAME) ||
      (process.env.GITHUB_EVENT_NAME === 'push' && mode !== 'sync')) {
    throw new Error('Run promotion or sync from main in UTMIST/Misty. Push events only sync.');
  }
  const report = (message) => {
    console.log(message);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${message}\n\n`);
  };
  const options = { repo: process.env.GITHUB_REPOSITORY, report };
  await sync(options);
  if (mode === 'promote') openPromotion(options);
}

module.exports = { prepareSync, assertSnapshot, sync, openPromotion };
if (require.main === module) main().catch((error) => {
  console.error(error.message);
  if (error.stderr) console.error(error.stderr.toString());
  process.exitCode = 1;
});
