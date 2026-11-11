import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { checkRepository } from "./check-docs.mjs";

const checker = fileURLToPath(new URL("./check-docs.mjs", import.meta.url));

function repo(t, files) {
  const root = mkdtempSync(join(tmpdir(), "misty-docs-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  execFileSync("git", ["init", "--quiet"], { cwd: root });
  for (const [name, contents] of Object.entries(files)) {
    mkdirSync(dirname(join(root, name)), { recursive: true });
    writeFileSync(join(root, name), contents);
  }
  return root;
}

test("reports missing files and anchors with source lines, including reference links", (t) => {
  const root = repo(t, {
    "README.md":
      "# Start\n[missing](docs/missing.md)\n[section][guide]\n\n[guide]: docs/guide.md#gone\n",
    "docs/guide.md": "# Present\n",
  });
  assert.deepEqual(checkRepository(root).errors, [
    "README.md:2: missing target docs/missing.md",
    "README.md:3: missing anchor docs/guide.md#gone",
  ]);
});

test("accepts formatted headings, duplicate slugs, setext headings and explicit anchors", (t) => {
  const root = repo(t, {
    "README.md":
      "[one](guide.md#hello-api) [two](guide.md#hello-api-1)\n" +
      "[collision](guide.md#hello-api-1-1) [unicode](guide.md#café)\n" +
      "[setext](guide.md#install) [custom](guide.md#custom)\n",
    "guide.md":
      '# Hello `API`!\n# Hello API\n# Hello API-1\n# Café\n\nInstall\n---\n<a id="custom"></a>\n',
  });
  assert.deepEqual(checkRepository(root).errors, []);
  assert.equal(checkRepository(root).links, 6);
});

test("ignores example links in code and comments, but checks links with code labels", (t) => {
  const root = repo(t, {
    "README.md":
      "# Start\n`[example](missing.md)`\n````md\n[example](missing.md)\n```\n````\n" +
      "~~~\n[example](missing.md)\n~~~\n<!-- [example](missing.md) -->\n" +
      "    [example](missing.md)\n[`real`](absent.md)\n",
  });
  assert.deepEqual(checkRepository(root).errors, [
    "README.md:12: missing target absent.md",
  ]);
});

test("checks encoded paths, balanced parentheses, images and HTML links offline", (t) => {
  const root = repo(t, {
    "docs/guide.md":
      "[file](../a%20file.md#title) [nested](../a(b).md)\n" +
      "[root](/a%20file.md) ![image](<../an image.png>)\n" +
      '<a href="../a%20file.md#title">link</a> <img src="missing.png">\n' +
      "[remote](https://example.invalid/gone#missing) [mail](mailto:test@example.invalid)\n",
    "a file.md": "# Title\n",
    "a(b).md": "# Parentheses\n",
    "an image.png": "fixture",
  });
  assert.deepEqual(checkRepository(root).errors, [
    "docs/guide.md:3: missing target missing.png",
  ]);
});

test("resolves full, collapsed and shortcut references and ignores unused definitions", (t) => {
  const root = repo(t, {
    "README.md":
      '[Full][Guide] [guide][] [guide]\n\n[Guide]: <guide.md#start> "Guide"\n' +
      "[unused]: missing.md\n",
    "guide.md": "# Start\n",
  });
  assert.deepEqual(checkRepository(root).errors, []);
  assert.equal(checkRepository(root).links, 3);
});

test("checks indented list continuations while ignoring code nested under a list", (t) => {
  const root = repo(t, {
    "README.md":
      "1. Read this.\n\n    [link](missing.md)\n\n" +
      "        [example](code.md)\n\nA new paragraph.\n\n    [example](code.md)\n",
  });
  assert.deepEqual(checkRepository(root).errors, [
    "README.md:3: missing target missing.md",
  ]);
});

test("includes new docs, ignores ignored files, and exits nonzero for a broken link", (t) => {
  const root = repo(t, {
    ".gitignore": "scratch/\n",
    "README.md": "[new](new.md)\n",
    "new.md": "[bad](missing.md)\n",
    "scratch/ignored.md": "[bad](also-missing.md)\n",
  });
  const result = spawnSync(process.execPath, [checker], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /new\.md:1: missing target missing\.md/);
  assert.doesNotMatch(result.stderr, /also-missing/);
  writeFileSync(join(root, "new.md"), "# New\n");
  const fixed = spawnSync(process.execPath, [checker], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(fixed.status, 0, fixed.stderr);
  assert.match(fixed.stdout, /2 Markdown files/);
});
