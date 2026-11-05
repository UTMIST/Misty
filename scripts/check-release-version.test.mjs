import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const checker = fileURLToPath(new URL('./check-release-version.mjs', import.meta.url));

function repo(t, version = '1.0.0\n') {
  const cwd = mkdtempSync(join(tmpdir(), 'misty-version-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const git = (...args) =>
    execFileSync('git', args, { cwd, encoding: 'utf8', stdio: 'pipe' }).trim();
  git('init', '--quiet');
  git('config', 'user.name', 'Release guard test');
  git('config', 'user.email', 'release-test@example.invalid');
  git('config', 'commit.gpgsign', 'false');
  git('config', 'tag.gpgsign', 'false');
  git('config', 'core.hooksPath', '/dev/null');
  if (version !== null) writeFileSync(join(cwd, 'VERSION'), version);
  git('add', '.');
  git('commit', '--quiet', '--allow-empty', '-m', 'Fixture');
  return {
    cwd,
    git,
    setVersion: (value) => writeFileSync(join(cwd, 'VERSION'), value),
    check: (...args) => spawnSync(process.execPath, [checker, ...args], { cwd, encoding: 'utf8' }),
  };
}

function expectPass(result) {
  assert.equal(result.status, 0, result.stderr);
}

function expectFailure(result, message) {
  assert.equal(result.status, 1, result.stdout);
  assert.match(result.stderr, message);
}

test('bootstraps versioning on a base without VERSION', (t) => {
  const r = repo(t, null);
  r.git('tag', '-a', 'v1.0.0', '-m', 'Historical baseline');
  r.setVersion('1.0.0\n');
  expectFailure(r.check('--base', 'HEAD'), /newer than latest release tag/);
  r.setVersion('1.0.1\n');
  expectPass(r.check('--base', 'HEAD', '--promotion'));
});

test('accepts a patch promotion and compares numeric components', (t) => {
  const r = repo(t, '1.0.9\n');
  r.git('tag', 'v1.0.9');
  r.git('tag', 'v1.0.8');
  r.setVersion('1.0.10\n');
  expectPass(r.check('--base', 'HEAD', '--promotion'));
});

test('keeps ordinary staging PRs and queued merges working after a release', (t) => {
  const r = repo(t);
  r.git('tag', 'v1.0.0');
  expectPass(r.check('--base', 'HEAD'));
  expectPass(r.check()); // Push/rerun after the matching tag is published.
  expectFailure(r.check('--base', 'HEAD', '--promotion'), /greater than base version/);
});

test('requires a bump for promotion even before the previous release is tagged', (t) => {
  const r = repo(t);
  expectFailure(r.check('--base', 'HEAD', '--promotion'), /greater than base version/);
});

test('rejects version regression against the base and published tags', (t) => {
  const r = repo(t, '1.2.0\n');
  r.setVersion('1.1.9\n');
  expectFailure(r.check('--base', 'HEAD'), /at least base version/);
  r.git('tag', 'v1.2.0');
  expectFailure(r.check(), /at least latest release tag/);
});

test('rejects a reserved version on another commit, including annotated tags', (t) => {
  const r = repo(t);
  r.git('tag', '-a', 'v1.0.1', '-m', 'Already released');
  r.setVersion('1.0.1\n');
  expectFailure(r.check('--base', 'HEAD'), /newer than latest release tag/);
  expectFailure(r.check('--promotion'), /newer than latest release tag/);
});

test('ignores unrelated and prerelease tags when finding stable releases', (t) => {
  const r = repo(t);
  r.git('tag', 'service-v9.0.0');
  r.git('tag', 'v2.0.0-rc.1');
  r.git('tag', 'v01.0.0');
  r.setVersion('1.0.1\n');
  expectPass(r.check('--base', 'HEAD', '--promotion'));
});

test('rejects malformed VERSION files', (t) => {
  const r = repo(t);
  const invalid = [
    'v1.0.0\n',
    '01.0.0\n',
    '1.0\n',
    '1.0.0-rc.1\n',
    '1.0.0+sha\n',
    ' 1.0.0\n',
    '1.0.0\n\n',
    '1.0.0\n2.0.0\n',
    '',
  ];
  for (const value of invalid) {
    r.setVersion(value);
    expectFailure(r.check(), /one MAJOR.MINOR.PATCH value/);
  }
  r.setVersion('1.0.0\r\n');
  expectPass(r.check());
});

test('fails on missing VERSION, unknown base, and incomplete history', (t) => {
  const r = repo(t, null);
  expectFailure(r.check(), /ENOENT/);
  r.setVersion('1.0.1\n');
  expectFailure(r.check('--base', 'missing-ref'), /Release version check failed/);
  writeFileSync(join(r.cwd, '.git', 'shallow'), `${r.git('rev-parse', 'HEAD')}\n`);
  expectFailure(r.check(), /Fetch full history and tags/);
});
