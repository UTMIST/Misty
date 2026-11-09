import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

test("provisioning selects the directory module from any working directory", (t) => {
  const cwd = mkdtempSync(join(tmpdir(), "misty-provision-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const log = join(cwd, "calls.jsonl");
  for (const name of ["uv", "railway"]) {
    writeFileSync(
      join(cwd, name),
      `#!/usr/bin/env node
const { appendFileSync } = require('node:fs');
appendFileSync(process.env.PROVISION_TEST_LOG, JSON.stringify({
  tool: '${name}', args: process.argv.slice(2), database: process.env.DATABASE_URL,
}) + '\\n');
if ('${name}' === 'uv') console.log('tt_fixture_synthetic');
`,
      { mode: 0o755 },
    );
  }
  const script = fileURLToPath(
    new URL("./provision-directory-key.sh", import.meta.url),
  );
  const result = spawnSync("bash", [script, "staging"], {
    cwd,
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${cwd}:${process.env.PATH}`,
      PROVISION_TEST_LOG: log,
      TT_DATABASE_URL: "postgresql://test.invalid/disposable",
    },
  });
  assert.equal(result.status, 0, result.stderr);
  const calls = readFileSync(log, "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  const uv = calls.filter((call) => call.tool === "uv");
  assert.equal(uv.length, 2);
  for (const call of uv) {
    assert.deepEqual(call.args.slice(0, 8), [
      "--directory",
      join(dirname(script), "../services/team-tracking"),
      "--project",
      ".",
      "run",
      "python",
      "-m",
      "src.cli",
    ]);
    assert.equal(call.database, "postgresql://test.invalid/disposable");
  }
  assert.deepEqual(
    calls.filter((call) => call.tool === "railway").map((call) => call.args),
    [
      [
        "variables",
        "--service",
        "discord-bot",
        "--environment",
        "staging",
        "--set",
        "DIRECTORY_API_KEY=tt_fixture_synthetic",
      ],
      [
        "variables",
        "--service",
        "documentation-system",
        "--environment",
        "staging",
        "--set",
        "DIRECTORY_API_KEY=tt_fixture_synthetic",
      ],
    ],
  );
});
