import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { DEPLOYMENT_TIMEOUT_MS, REPOSITORY } from './preview.js';

const exec = promisify(execFile);
// Public project identifier; credentials still come from Railway CLI login.
const DEFAULT_PROJECT_ID = 'abf9e8a2-9394-48e2-9d95-a71520c58a5b';

export function parsePreviewArgs(argv, env = process.env) {
  const args = [...argv];
  const number = args.shift();
  let projectId = env.MISTY_PREVIEW_PROJECT_ID || DEFAULT_PROJECT_ID;
  let planOnly = false;
  let confirmedIdle = false;
  let timeoutMs = DEPLOYMENT_TIMEOUT_MS;
  while (args.length) {
    const arg = args.shift();
    if (arg === '--project' || arg === '--timeout-minutes') {
      const value = args.shift();
      if (!value || value.startsWith('-')) throw new Error(`Missing value for ${arg}.`);
      if (arg === '--project') projectId = value;
      else {
        if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value) * 60_000)) {
          throw new Error('--timeout-minutes must be a positive whole number.');
        }
        timeoutMs = Number(value) * 60_000;
      }
    } else if (arg === '--plan') planOnly = true;
    else if (arg === '--recordings-stopped') confirmedIdle = true;
    else throw new Error(`Unknown option: ${arg}`);
  }
  if (!/^[1-9]\d*$/.test(number ?? '') || !projectId) {
    throw new Error(
      'Usage: npm run preview -- <pr> [--project <Railway-project-id>] [--timeout-minutes <minutes>] [--plan] [--recordings-stopped]',
    );
  }
  return { number, projectId, planOnly, confirmedIdle, timeoutMs };
}

export async function readPreviewSource(sha, run = exec) {
  return jsonCommand('gh', ['api', `repos/${REPOSITORY}/git/trees/${sha}?recursive=1`], run);
}

export async function jsonCommand(command, args, run = exec) {
  let stdout;
  try {
    ({ stdout } = await run(command, args, {
      maxBuffer: 4 * 1024 * 1024,
      timeout: 60_000,
    }));
  } catch (error) {
    let detail = error.stderr?.trim() || error.message;
    if (error.code === 'ENOENT') detail = 'executable not found on PATH; install the CLI.';
    else if (error.killed && error.signal) detail = `timed out after 60 seconds. ${detail}`;
    // These commands contain repository metadata, IDs, and SHAs, not secrets.
    throw new Error(`${command} failed: ${detail}`, { cause: error });
  }
  try {
    return JSON.parse(stdout);
  } catch (error) {
    throw new Error(`${command} returned invalid JSON.`, { cause: error });
  }
}

export async function railwayApi(query, variables, run = exec) {
  const result = await jsonCommand(
    'railway',
    ['api', query, '--variables', JSON.stringify(variables), '--compact', '--allow-errors'],
    run,
  );
  if (result.errors?.length) {
    throw new Error(`Railway API: ${result.errors.map((error) => error.message).join('; ')}`);
  }
  if (!result.data) throw new Error('Railway API returned no data.');
  return result.data;
}
