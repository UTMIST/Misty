import { spawn } from 'node:child_process';

export function buildSnapshotCommands({ source, target, dbUser }) {
  const dropCmd = [
    'docker',
    'compose',
    'exec',
    '-T',
    'postgres',
    'psql',
    '-U',
    dbUser,
    '-d',
    'postgres',
    '-c',
    `DROP DATABASE IF EXISTS ${target};`,
  ];
  const createCmd = [
    'docker',
    'compose',
    'exec',
    '-T',
    'postgres',
    'psql',
    '-U',
    dbUser,
    '-d',
    'postgres',
    '-c',
    `CREATE DATABASE ${target};`,
  ];
  // Pipe pg_dump into psql. `set -e -o pipefail` is critical: without it, if
  // pg_dump fails, psql receives an empty stream, exits 0, and the pipe
  // silently succeeds — leaving the scratch DB empty. With pipefail, any
  // failure in the pipeline surfaces as a non-zero exit.
  const pipeCmd = [
    'sh',
    '-c',
    'set -e -o pipefail; ' +
      `docker compose exec -T postgres pg_dump -U ${dbUser} ${source} | ` +
      `docker compose exec -T postgres psql -q -U ${dbUser} -d ${target}`,
  ];
  return [dropCmd, createCmd, pipeCmd];
}

function runCommand(argv, cwd) {
  return new Promise((resolve, reject) => {
    const [cmd, ...args] = argv;
    const child = spawn(cmd, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = '';
    child.stderr.on('data', (d) => {
      stderr += d.toString();
    });
    child.on('exit', (code) => {
      if (code === 0) resolve();
      else
        reject(new Error(`snapshot command failed (exit ${code}): ${argv.join(' ')}\n${stderr}`));
    });
  });
}

export async function snapshotDb({ source, target, dbUser, teamTrackingDir }) {
  const cmds = buildSnapshotCommands({ source, target, dbUser });
  for (const cmd of cmds) {
    await runCommand(cmd, teamTrackingDir);
  }
}
