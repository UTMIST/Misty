#!/usr/bin/env node
// Platform release guard. Run from the repository root; see docs/RELEASING.md.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';

const stableVersion = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;

function parseVersion(text) {
  const version = text.replace(/\r?\n$/, '');
  const match = stableVersion.exec(version);
  if (!match || match[0] !== version) {
    throw new Error(
      'VERSION must contain one MAJOR.MINOR.PATCH value, without a v prefix or suffix.',
    );
  }
  return version;
}

function compare(left, right) {
  const a = left.split('.').map(BigInt);
  const b = right.split('.').map(BigInt);
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1;
  }
  return 0;
}

function git(...args) {
  return execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}

try {
  const { values } = parseArgs({
    options: { base: { type: 'string' }, promotion: { type: 'boolean', default: false } },
  });
  const version = parseVersion(readFileSync('VERSION', 'utf8'));
  if (git('rev-parse', '--is-shallow-repository') !== 'false') {
    throw new Error('Fetch full history and tags before checking release versions.');
  }

  let baseVersion;
  if (values.base) {
    // Resolve first: an unavailable base must not look like the first release.
    const base = git('rev-parse', '--verify', '--end-of-options', `${values.base}^{commit}`);
    if (git('ls-tree', '--name-only', base, '--', 'VERSION')) {
      baseVersion = parseVersion(git('show', `${base}:VERSION`));
      const order = compare(version, baseVersion);
      if (order < 0 || (values.promotion && order === 0)) {
        const requirement = values.promotion ? 'greater than' : 'at least';
        throw new Error(`VERSION ${version} must be ${requirement} base version ${baseVersion}.`);
      }
    }
  }

  const tags = git('tag', '--list', 'v*')
    .split('\n')
    .map((tag) => tag.slice(1))
    .filter((tag) => stableVersion.test(tag));
  const latest = tags.sort(compare).at(-1);
  // Ordinary staging work may retain the released number until release prep.
  // A promotion or a changed VERSION must reserve a newer, unused number.
  const needsNewVersion =
    values.promotion || (values.base !== undefined && version !== baseVersion);
  if (latest && (compare(version, latest) < 0 || (needsNewVersion && version === latest))) {
    const requirement = needsNewVersion ? 'newer than' : 'at least';
    throw new Error(`VERSION ${version} must be ${requirement} latest release tag v${latest}.`);
  }
  console.log(`Release version ${version} is valid${values.promotion ? ' for promotion' : ''}.`);
} catch (error) {
  console.error(`Release version check failed: ${error.message}`);
  process.exitCode = 1;
}
