#!/usr/bin/env node
/**
 * Label taxonomy consistency checker.
 *
 * Two hand-maintained lists, each copied across several files. This script
 * cross-checks them so a value added (or renamed) in one place but not the
 * others fails CI instead of failing silently — the gap named in
 * docs/CODE-OWNERSHIP.md.
 *
 * ZONES are the PR axis: which directory bucket a change lands in, and so who
 * reviews it. Single-valued — one file, one zone.
 *   Canonical: the `zone_for()` case arms in .github/workflows/pr-zone-check.yml,
 *   because that function is what actually decides a PR's zones at review time.
 *   Checked against:
 *     - .github/CODEOWNERS               (one line per owned zone)
 *     - .github/labeler.yml              (`zone: <name>` label keys)
 *     - .github/PULL_REQUEST_TEMPLATE.md (the Zone list)
 *     - docs/CODE-OWNERSHIP.md           (the canonical table)
 *     - services/* and packages/* on disk (every directory has a zone)
 *
 * AREAS are the ISSUE axis: which parts of the system a piece of work involves.
 * Multi-valued — most issues here carry two or three.
 *   Canonical: the `areas` array in .github/workflows/area-label-issues.yml,
 *   because that array is what actually applies the labels.
 *   Checked against:
 *     - .github/ISSUE_TEMPLATE/*.yml     (each Area dropdown's options)
 *
 * The two are deliberately independent: one `area/service` issue can span six
 * zones. Nothing here checks that the GitHub labels themselves exist — a
 * missing one is created on first use, in a random colour.
 *
 * The disk check is the one that matters most: in August 2026 five service
 * directories had no zone entry and collapsed into the `services/*` catch-all,
 * so a PR spanning llm and meeting counted as a single zone and never warned.
 *
 * Usage: node scripts/check-labels.mjs [repo-root]
 * (repo-root defaults to the parent of this script's directory; the override
 * exists for testing against a doctored copy of the tree.)
 */

import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(
  process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), ".."),
);

const FILES = {
  zoneCheck: ".github/workflows/pr-zone-check.yml",
  codeowners: ".github/CODEOWNERS",
  labeler: ".github/labeler.yml",
  prTemplate: ".github/PULL_REQUEST_TEMPLATE.md",
  ownershipDoc: "docs/CODE-OWNERSHIP.md",
  areaWorkflow: ".github/workflows/area-label-issues.yml",
};

// Issue forms carrying an Area dropdown. Each must offer every area plus a
// "not sure" escape — a required dropdown with no escape makes people guess,
// and a guessed label is worse than an unlabelled issue.
const ISSUE_FORMS = [
  ".github/ISSUE_TEMPLATE/bug_report.yml",
  ".github/ISSUE_TEMPLATE/feature_request.yml",
  ".github/ISSUE_TEMPLATE/epic.yml",
];

// Buckets a PR can legitimately land in but which are never *chosen* from a
// menu: reaching one means something was added that nobody registered, and the
// fix is to give it a real zone rather than pick the catch-all.
const TRANSITIONAL = new Set(["packages/other", "services/other"]);

// Zones with no CODEOWNERS line of their own: they resolve through the `*`
// fallback. Latent while every owner is the same person; see the doc.
const FALLBACK_OWNED = new Set(["packages/other", "services/other", "root"]);

// Workspace parents whose children must each carry their own zone.
const WORKSPACE_DIRS = ["services", "packages"];

// Anchored on purpose. Unanchored, this matches `services/other` and
// `packages/other` on the "other" alternative, which would quietly excuse the
// exact two buckets the menus are supposed to never offer.
const ESCAPE_RE = /^(not\s*sure|unsure|other|unknown|don'?t\s*know)$/i;

const errors = [];
const fail = (msg) => errors.push(msg);

function readOrDie(relPath) {
  const abs = join(ROOT, relPath);
  try {
    // Normalise CRLF: the repo is developed on Windows too, and every parser
    // below is line-oriented.
    return readFileSync(abs, "utf8").replace(/\r\n/g, "\n");
  } catch {
    console.error(`FAIL: cannot read ${relPath} (looked in ${ROOT})`);
    process.exit(1);
  }
}

// Pull the backticked entries out of a template's "`a` · `b` · `c`" line.
function zoneListFrom(src, relPath) {
  const line = src.split("\n").find((l) => l.includes("·") && l.includes("`"));
  if (!line) {
    fail(`${relPath}: could not find the zone list (a line of \`zone\` · \`zone\`)`);
    return null;
  }
  return [...line.matchAll(/`([^`]+)`/g)].map((m) => m[1]);
}

// Pull the `options:` of a dropdown with the given id out of an issue form.
// Hand-rolled rather than a YAML parse so this script keeps zero dependencies.
function dropdownOptionsFrom(src, relPath, id) {
  const lines = src.split("\n");
  const anchor = lines.findIndex((l) => new RegExp(`^\\s*id:\\s*${id}\\s*$`).test(l));
  if (anchor === -1) {
    fail(`${relPath}: no ${id} dropdown (expected a body item with 'id: ${id}')`);
    return null;
  }
  // Stop at the next body item. Unbounded, a missing `options:` on this field
  // would silently pick up the NEXT field's options and report nonsense.
  const nextItem = lines.findIndex((l, i) => i > anchor && /^\s*-\s*type:\s*\S/.test(l));
  const limit = nextItem === -1 ? lines.length : nextItem;
  const optIdx = lines.findIndex(
    (l, i) => i > anchor && i < limit && /^\s*options:\s*$/.test(l),
  );
  if (optIdx === -1) {
    fail(`${relPath}: the ${id} dropdown has no 'options:' block`);
    return null;
  }
  const optIndent = lines[optIdx].match(/^\s*/)[0].length;
  const options = [];
  for (let i = optIdx + 1; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*$/.test(line) || /^\s*#/.test(line)) continue;
    if (line.match(/^\s*/)[0].length <= optIndent) break; // dedent ends the block
    const m = line.match(/^\s*-\s*['"]?(.+?)['"]?\s*$/);
    if (!m) break;
    options.push(m[1]);
  }
  if (options.length === 0) {
    fail(`${relPath}: the ${id} dropdown's 'options:' block is empty`);
    return null;
  }
  return options;
}

// Pull a `name = [ "a", "b" ]` literal out of a workflow's inline script.
function arrayLiteralFrom(src, relPath, name) {
  const m = src.match(new RegExp(`\\b${name}\\s*=\\s*\\[([\\s\\S]*?)\\]`));
  if (!m) {
    fail(`${relPath}: could not find a '${name} = [...]' array — has the workflow's shape changed?`);
    return null;
  }
  const out = [...m[1].matchAll(/["'`]([^"'`]+)["'`]/g)].map((x) => x[1]);
  if (out.length === 0) {
    fail(`${relPath}: the '${name}' array contains no strings`);
    return null;
  }
  return out;
}

// ===========================================================================
// ZONES
// ===========================================================================

// ---------------------------------------------------------------------------
// a. Canonical zones: the `zone_for()` case arms in pr-zone-check.yml
// ---------------------------------------------------------------------------
const zoneCheckSrc = readOrDie(FILES.zoneCheck);
const arms = [];
{
  const start = zoneCheckSrc.indexOf("zone_for()");
  if (start === -1) {
    console.error(`FAIL: no zone_for() function in ${FILES.zoneCheck}`);
    process.exit(1);
  }
  const body = zoneCheckSrc.slice(start);
  const end = body.indexOf("esac");
  if (end === -1) {
    console.error(`FAIL: zone_for() in ${FILES.zoneCheck} has no closing 'esac'`);
    process.exit(1);
  }
  // `  services/llm/*)   echo services/llm ;;`
  for (const m of body.slice(0, end).matchAll(/^\s*([^)\s|]+)\)\s*echo\s+(\S+)\s*;;/gm)) {
    arms.push({ pattern: m[1], zone: m[2] });
  }
}
if (arms.length === 0) {
  console.error(
    `FAIL: parsed no case arms out of zone_for() in ${FILES.zoneCheck} — ` +
      `has the function's shape changed?`,
  );
  process.exit(1);
}

const canonical = arms.map((a) => a.zone);
const canonicalSet = new Set(canonical);
if (canonicalSet.size !== canonical.length) {
  const dupes = canonical.filter((z, i) => canonical.indexOf(z) !== i);
  fail(`${FILES.zoneCheck}: zone_for() emits duplicate zones: ${[...new Set(dupes)].join(", ")}`);
}

// The menu the PR template is expected to offer.
const selectable = canonical.filter((z) => !TRANSITIONAL.has(z));

// ---------------------------------------------------------------------------
// b. Ordering: zone_for() is first-match-wins, so catch-alls must come last
// ---------------------------------------------------------------------------
{
  for (const [i, arm] of arms.entries()) {
    // `services/*` and `packages/*` are catch-alls; `*` is the default arm.
    const catchAll = arm.pattern.match(/^([^/]+)\/\*$/);
    if (!catchAll) continue;
    const prefix = `${catchAll[1]}/`;
    for (const [j, other] of arms.entries()) {
      if (j <= i) continue;
      if (other.pattern.startsWith(prefix)) {
        fail(
          `${FILES.zoneCheck}: '${other.pattern}' is listed after the ` +
            `'${arm.pattern}' catch-all, so zone_for() can never reach it ` +
            `(first match wins — move it above)`,
        );
      }
    }
  }
  const defaultIdx = arms.findIndex((a) => a.pattern === "*");
  if (defaultIdx === -1) {
    fail(`${FILES.zoneCheck}: zone_for() has no '*)' default arm — a file could get no zone`);
  } else if (defaultIdx !== arms.length - 1) {
    fail(`${FILES.zoneCheck}: the '*)' default arm must be last in zone_for()`);
  }
}

// ---------------------------------------------------------------------------
// c. CODEOWNERS has a line per owned zone, and no lines for unknown zones
// ---------------------------------------------------------------------------
{
  const src = readOrDie(FILES.codeowners);
  const owned = new Set();
  for (const line of src.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const [pattern] = trimmed.split(/\s+/);
    if (pattern === "*") continue; // the fallback
    // `/services/llm/` -> `services/llm`
    owned.add(pattern.replace(/^\/+/, "").replace(/\/+$/, ""));
  }
  for (const zone of canonical) {
    if (FALLBACK_OWNED.has(zone)) {
      if (owned.has(zone)) {
        fail(
          `zone '${zone}' has a ${FILES.codeowners} line, but it is listed as ` +
            `falling through to the '*' fallback — update FALLBACK_OWNED in ` +
            `this script and the "Latent, not yet biting" note in ${FILES.ownershipDoc}`,
        );
      }
      continue;
    }
    if (!owned.has(zone)) {
      fail(`zone '${zone}' is in zone_for() but has no line in ${FILES.codeowners}`);
    }
  }
  for (const pattern of owned) {
    if (!canonicalSet.has(pattern)) {
      fail(
        `${FILES.codeowners} owns '/${pattern}/' but zone_for() in ` +
          `${FILES.zoneCheck} has no matching zone`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// d. labeler.yml defines exactly one label per zone
// ---------------------------------------------------------------------------
{
  const src = readOrDie(FILES.labeler);
  const labelled = [];
  for (const line of src.split("\n")) {
    // Top-level key: `"zone: services/llm":`
    const m = line.match(/^['"]?zone:\s*([^'"]+?)['"]?\s*:\s*$/);
    if (m) labelled.push(m[1].trim());
  }
  if (labelled.length === 0) {
    fail(`${FILES.labeler}: found no 'zone: <name>' label keys — has the file's shape changed?`);
  } else {
    for (const zone of canonical) {
      if (!labelled.includes(zone)) {
        fail(`zone '${zone}' is in zone_for() but has no 'zone: ${zone}' label in ${FILES.labeler}`);
      }
    }
    for (const zone of labelled) {
      if (!canonicalSet.has(zone)) {
        fail(`${FILES.labeler} defines 'zone: ${zone}' but zone_for() never emits it`);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// d2. labeler.yml's GLOBS resolve each zone the same way zone_for() does
//
// Checking the keys alone is not enough. The catch-all buckets work by
// negation, so adding `zone: services/scheduler` without also adding
// `!services/scheduler/**` to the `zone: services/other` bucket leaves every
// scheduler PR carrying BOTH labels — the exact "unregistered service hides in
// the catch-all" confusion this script exists to kill. A typo'd glob
// (`services/lmm/**`) is invisible to a key comparison too.
// ---------------------------------------------------------------------------
{
  const src = readOrDie(FILES.labeler);

  // Parse `zone: <name>:` -> [{ mode, globs }]. A zone may carry several match
  // configs; labeler ORs them.
  const rules = new Map();
  let zone = null;
  let current = null;
  for (const line of src.split("\n")) {
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const key = line.match(/^['"]?zone:\s*([^'"]+?)['"]?\s*:\s*$/);
    if (key) {
      zone = key[1].trim();
      rules.set(zone, []);
      current = null;
      continue;
    }
    const mode = line.match(/^\s*-\s*(any-glob-to-any-file|all-globs-to-any-file):\s*$/);
    if (mode && zone) {
      current = { mode: mode[1], globs: [] };
      rules.get(zone).push(current);
      continue;
    }
    const g = line.match(/^\s*-\s*["']?(!?[^"'\s]+)["']?\s*$/);
    if (g && current && g[1].includes("*")) current.globs.push(g[1]);
  }

  // This evaluator only understands `**` and `<prefix>/**` (with an optional
  // leading `!`). Anything else and the verification below would be quietly
  // wrong, so refuse rather than pass.
  const understood = (glob) => {
    const pat = glob.startsWith("!") ? glob.slice(1) : glob;
    return pat === "**" || pat.endsWith("/**");
  };
  const matches = (glob, path) => {
    const neg = glob.startsWith("!");
    const pat = neg ? glob.slice(1) : glob;
    const hit = pat === "**" ? true : path.startsWith(pat.slice(0, -2));
    return neg ? !hit : hit;
  };

  let evaluable = true;
  for (const [z, configs] of rules) {
    for (const { globs } of configs) {
      for (const glob of globs) {
        if (!understood(glob)) {
          fail(
            `${FILES.labeler}: glob '${glob}' under 'zone: ${z}' is not of the ` +
              `form '**' or '<prefix>/**', which is all this check can verify — ` +
              `either simplify it or extend the evaluator in ${"scripts/check-labels.mjs"}`,
          );
          evaluable = false;
        }
      }
    }
  }

  if (evaluable && rules.size > 0) {
    const zonesForPath = (path) =>
      [...rules]
        .filter(([, configs]) =>
          configs.some(({ mode, globs }) =>
            mode === "any-glob-to-any-file"
              ? globs.some((g) => matches(g, path))
              : globs.every((g) => matches(g, path)),
          ),
        )
        .map(([z]) => z);

    // One probe path per zone_for() arm: `services/llm/*` -> a file inside it,
    // `services/*` -> a file in an unregistered service, `*` -> a root file.
    for (const { pattern, zone: expected } of arms) {
      const probe =
        pattern === "*"
          ? "__zoneprobe__.txt"
          : `${pattern.replace(/\*$/, "")}__zoneprobe__/file.txt`;
      const got = zonesForPath(probe);
      if (got.length === 1 && got[0] === expected) continue;
      if (got.length === 0) {
        fail(
          `${FILES.labeler}: a file at '${probe}' gets NO zone label, but ` +
            `zone_for() calls it '${expected}' — the globs for 'zone: ${expected}' ` +
            `do not cover it`,
        );
      } else if (got.length > 1) {
        fail(
          `${FILES.labeler}: a file at '${probe}' gets ${got.length} zone labels ` +
            `(${got.join(", ")}) but zone_for() calls it '${expected}'. A file must ` +
            `land in exactly one zone — add the missing '!' negation to the ` +
            `catch-all bucket(s) it is wrongly matching`,
        );
      } else {
        fail(
          `${FILES.labeler}: a file at '${probe}' is labelled '${got[0]}' but ` +
            `zone_for() calls it '${expected}' — the two disagree`,
        );
      }
    }
  }
}

// ---------------------------------------------------------------------------
// e. the PR template lists exactly the selectable zones
// ---------------------------------------------------------------------------
{
  const relPath = FILES.prTemplate;
  const listed = zoneListFrom(readOrDie(relPath), relPath);
  if (listed) {
    for (const zone of selectable) {
      if (!listed.includes(zone)) {
        fail(`zone '${zone}' is in zone_for() but missing from the zone list in ${relPath}`);
      }
    }
    for (const zone of listed) {
      if (TRANSITIONAL.has(zone)) {
        fail(
          `${relPath} offers '${zone}', which is a transitional bucket — the ` +
            `template deliberately omits it (see ${FILES.ownershipDoc})`,
        );
      } else if (!canonicalSet.has(zone)) {
        fail(`${relPath} offers '${zone}', which zone_for() never emits`);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// f. every zone has a row in the CODE-OWNERSHIP.md table
// ---------------------------------------------------------------------------
{
  const src = readOrDie(FILES.ownershipDoc);
  for (const zone of canonical) {
    if (!src.includes(`| \`${zone}\` |`)) {
      fail(`zone '${zone}' has no row in the ${FILES.ownershipDoc} table`);
    }
  }
}

// ---------------------------------------------------------------------------
// g. every services/* and packages/* directory carries its own zone
// ---------------------------------------------------------------------------
for (const parent of WORKSPACE_DIRS) {
  const abs = join(ROOT, parent);
  if (!existsSync(abs)) {
    fail(`${parent}/ does not exist — cannot check that its members have zones`);
    continue;
  }
  const children = readdirSync(abs, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
  for (const child of children) {
    const zone = `${parent}/${child}`;
    if (!canonicalSet.has(zone)) {
      fail(
        `${zone}/ exists on disk but has no zone — it falls through to the ` +
          `'${parent}/*' catch-all, where it shares a bucket with every other ` +
          `unregistered ${parent} member and multi-zone PRs stop warning. ` +
          `Give it a zone (see ${FILES.ownershipDoc} -> "Adding or renaming a zone")`,
      );
    }
  }
}

// ===========================================================================
// AREAS
// ===========================================================================

// ---------------------------------------------------------------------------
// h. Canonical areas: the `areas` array in area-label-issues.yml
// ---------------------------------------------------------------------------
const areas = arrayLiteralFrom(readOrDie(FILES.areaWorkflow), FILES.areaWorkflow, "areas");
const areaSet = new Set(areas ?? []);
if (areas && areaSet.size !== areas.length) {
  const dupes = areas.filter((a, i) => areas.indexOf(a) !== i);
  fail(`${FILES.areaWorkflow}: the areas array has duplicates: ${[...new Set(dupes)].join(", ")}`);
}

// ---------------------------------------------------------------------------
// i. every issue form's Area dropdown offers the areas + an escape
// ---------------------------------------------------------------------------
if (areas) {
  for (const relPath of ISSUE_FORMS) {
    const options = dropdownOptionsFrom(readOrDie(relPath), relPath, "area");
    if (!options) continue;
    const offered = options.filter((o) => !ESCAPE_RE.test(o));
    for (const area of areas) {
      if (!offered.includes(area)) {
        fail(`area '${area}' is in ${FILES.areaWorkflow} but missing from the Area dropdown in ${relPath}`);
      }
    }
    for (const area of offered) {
      if (!areaSet.has(area)) {
        fail(`${relPath} offers area '${area}', which ${FILES.areaWorkflow} never applies`);
      }
    }
    if (!options.some((o) => ESCAPE_RE.test(o))) {
      fail(`${relPath}: the Area dropdown has no 'not sure'-style escape option`);
    }
  }
}

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------
if (errors.length > 0) {
  console.error("Label consistency check FAILED:\n");
  for (const e of errors) console.error(`  - ${e}`);
  console.error(
    `\nZones are hand-maintained across ${Object.keys(FILES).length - 1} files ` +
      `(zone_for() in ${FILES.zoneCheck} is canonical); areas across ` +
      `${ISSUE_FORMS.length + 1} (${FILES.areaWorkflow} is canonical). ` +
      `docs/CODE-OWNERSHIP.md -> "Adding or renaming a zone" lists them.`,
  );
  process.exit(1);
}

console.log(`Label consistency check passed.\n\nZones (${canonical.length}) — the PR axis:`);
for (const zone of canonical) {
  const notes = [];
  if (TRANSITIONAL.has(zone)) notes.push("transitional");
  if (FALLBACK_OWNED.has(zone)) notes.push("* fallback owner");
  console.log(`  - ${zone}${notes.length ? `  (${notes.join(", ")})` : ""}`);
}
console.log(`\nAreas (${areas?.length ?? 0}) — the issue axis:`);
for (const area of areas ?? []) console.log(`  - area/${area}`);
