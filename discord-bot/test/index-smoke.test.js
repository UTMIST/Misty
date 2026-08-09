import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// index.js is the only module with no coverage, and the one whose failure is a
// total outage. A factory used but never imported passed the entire suite and
// would have surfaced only on deploy, as
// `ReferenceError: makeChannelNotifier is not defined`.
//
// It cannot be import-tested: importing runs main(), which process.exit(1)s on
// a bad token and fails the runner regardless. So check it statically -- every
// factory-style call must resolve to something imported or declared locally.
test('every factory index.js calls is imported or declared', () => {
  const src = readFileSync(fileURLToPath(new URL('../src/index.js', import.meta.url)), 'utf8');

  const available = new Set();
  for (const m of src.matchAll(/import\s*\{([^}]+)\}/g)) {
    for (const name of m[1].split(','))
      available.add(
        name
          .trim()
          .split(/\s+as\s+/)
          .pop(),
      );
  }
  for (const m of src.matchAll(/\b(?:function|const|let|var|class)\s+(\w+)/g)) available.add(m[1]);

  const called = new Set(
    [...src.matchAll(/\b((?:make|create|wire)[A-Z]\w*)\s*\(/g)].map((m) => m[1]),
  );

  const missing = [...called].filter((name) => !available.has(name));
  assert.deepEqual(missing, [], `index.js calls undeclared: ${missing.join(', ')}`);
});
