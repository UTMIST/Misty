import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);

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
