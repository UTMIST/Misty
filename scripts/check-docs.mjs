// Offline checks for repository Markdown links and GitHub-style heading anchors.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const blank = (text) => text.replace(/[^\n]/g, " ");
const label = (text) => text.trim().replace(/\s+/g, " ").toLowerCase();

function prose(source) {
  let fence;
  let listIndent = 0;
  return source
    .replace(/<!--[\s\S]*?-->/g, blank)
    .split("\n")
    .map((line) => {
      const marker = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
      if (fence) {
        if (
          marker &&
          marker[1][0] === fence[0] &&
          marker[1].length >= fence.length &&
          !marker[2].trim()
        )
          fence = undefined;
        return blank(line);
      }
      if (marker) {
        fence = marker[1];
        return blank(line);
      }
      const item = line.match(/^(\s*)(?:[-*+]|\d+[.)])\s+/);
      const indent = line.match(/^\s*/)[0].replace(/\t/g, "    ").length;
      if (item) listIndent = item[0].length;
      else if (line.trim() && indent < listIndent) listIndent = 0;
      if (!item && indent >= listIndent + 4) return blank(line);
      return line;
    })
    .join("\n");
}

function entities(text) {
  const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
  return text.replace(
    /&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos);/gi,
    (whole, entity) => {
      if (!entity.startsWith("#")) return named[entity.toLowerCase()];
      const point =
        entity[1].toLowerCase() === "x"
          ? parseInt(entity.slice(2), 16)
          : Number(entity.slice(1));
      return point <= 0x10ffff ? String.fromCodePoint(point) : whole;
    },
  );
}

function anchors(source) {
  const text = prose(source);
  const ids = new Set();
  for (const match of text.matchAll(/\b(?:id|name)=["']([^"']+)["']/g))
    ids.add(match[1]);
  const used = new Set();
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const atx = lines[i].match(/^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$/);
    const setext =
      i + 1 < lines.length && /^\s{0,3}(?:=+|-+)\s*$/.test(lines[i + 1]);
    if (!atx && !(setext && lines[i].trim())) continue;
    const heading = entities(
      (atx ? atx[1] : lines[i].trim())
        .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
        .replace(/<[^>]*>/g, ""),
    );
    const base = heading
      .toLowerCase()
      .replace(/[^\p{L}\p{M}\p{N}_\- ]/gu, "")
      .replace(/ /g, "-");
    let slug = base;
    for (let suffix = 1; used.has(slug); suffix++) slug = `${base}-${suffix}`;
    used.add(slug);
    ids.add(slug);
  }
  return ids;
}

// Read a Markdown destination, preserving balanced/escaped parentheses and <spaces>.
function destination(text, start) {
  let i = start;
  while (/\s/.test(text[i] || "") && i < text.length) i++;
  if (text[i] === "<") {
    const end = text.indexOf(">", i + 1);
    return end < 0 ? null : { url: text.slice(i + 1, end), end: end + 1 };
  }
  const begin = i;
  let depth = 0;
  for (; i < text.length; i++) {
    if (text[i] === "\\") {
      i++;
      continue;
    }
    if (text[i] === "(") depth++;
    else if (text[i] === ")") {
      if (!depth) break;
      depth--;
    } else if (/\s/.test(text[i])) break;
  }
  return { url: text.slice(begin, i), end: i };
}

function links(source) {
  const text = prose(source).replace(/(`+)([\s\S]*?)\1(?!`)/g, blank);
  const references = new Map();
  const body = text.replace(
    /^ {0,3}\[([^\]]+)\]:[ \t]*(.*)$/gm,
    (whole, name, value) => {
      const target = destination(value, 0);
      if (target && !references.has(label(name)))
        references.set(label(name), target.url);
      return blank(whole);
    },
  );
  const found = [];
  for (const match of body.matchAll(/!?\[([^\]\n]*)\]/g)) {
    if (match.index > 0 && body[match.index - 1] === "\\") continue;
    const next = match.index + match[0].length;
    let url;
    if (body[next] === "(") {
      url = destination(body, next + 1)?.url;
    } else if (body[next] === "[") {
      const end = body.indexOf("]", next + 1);
      if (end >= 0)
        url = references.get(label(body.slice(next + 1, end) || match[1]));
    } else if (body[match.index - 1] !== "]") {
      url = references.get(label(match[1]));
    }
    if (url !== undefined) found.push({ url, offset: match.index });
  }
  for (const match of body.matchAll(
    /<(?:a|img)\b[^>]*?\b(?:href|src)=["']([^"']+)["'][^>]*>/gi,
  )) {
    found.push({ url: match[1], offset: match.index });
  }
  return found
    .sort((a, b) => a.offset - b.offset)
    .map((item) => ({
      url: entities(item.url.replace(/\\([\\()[\] <>])/g, "$1")),
      line: body.slice(0, item.offset).split("\n").length,
    }));
}

export function checkRepository(root = process.cwd()) {
  const paths = execFileSync(
    "git",
    ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    { cwd: root, encoding: "utf8" },
  ).split("\0");
  const files = [...new Set(paths)]
    .filter((p) => /\.md$/i.test(p) && existsSync(resolve(root, p)))
    .sort();
  const result = { files: files.length, links: 0, errors: [] };
  const anchorCache = new Map();
  for (const file of files) {
    for (const { url, line } of links(
      readFileSync(resolve(root, file), "utf8"),
    )) {
      if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(url)) continue;
      result.links++;
      const [address, fragment] = url.split("#");
      let path, anchor;
      try {
        path = decodeURIComponent(address.split("?")[0]);
        anchor =
          fragment === undefined ? undefined : decodeURIComponent(fragment);
      } catch {
        result.errors.push(`${file}:${line}: invalid URL encoding ${url}`);
        continue;
      }
      const target = path.startsWith("/")
        ? resolve(root, `.${path}`)
        : path
          ? resolve(root, dirname(file), path)
          : resolve(root, file);
      if (!existsSync(target)) {
        result.errors.push(`${file}:${line}: missing target ${url}`);
      } else if (
        anchor &&
        extname(target).toLowerCase() === ".md" &&
        statSync(target).isFile()
      ) {
        if (!anchorCache.has(target))
          anchorCache.set(target, anchors(readFileSync(target, "utf8")));
        if (!anchorCache.get(target).has(anchor))
          result.errors.push(`${file}:${line}: missing anchor ${url}`);
      }
    }
  }
  return result;
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    const result = checkRepository();
    for (const error of result.errors) console.error(error);
    console.log(
      `Checked ${result.files} Markdown files and ${result.links} local links.`,
    );
    process.exitCode = result.errors.length ? 1 : 0;
  } catch (error) {
    console.error(`Documentation check failed: ${error.message}`);
    process.exitCode = 1;
  }
}
