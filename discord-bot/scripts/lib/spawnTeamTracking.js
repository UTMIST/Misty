import { spawn } from 'node:child_process';

async function pollReady(url, { attempts = 30, delayMs = 250 } = {}) {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  throw new Error(`team-tracking at ${url} did not become ready within ${attempts * delayMs}ms`);
}

export async function spawnTeamTracking({ port, databaseUrl, teamTrackingDir }) {
  const child = spawn(
    'uv',
    ['run', 'uvicorn', 'src.api.app:app', '--port', String(port), '--log-level', 'warning'],
    {
      cwd: teamTrackingDir,
      env: { ...process.env, DATABASE_URL: databaseUrl, TT_ENV: 'local' },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  child.stdout.on('data', (d) => process.stdout.write(`[tt:${port}] ${d}`));
  child.stderr.on('data', (d) => process.stderr.write(`[tt:${port}] ${d}`));
  const url = `http://127.0.0.1:${port}`;
  try {
    await pollReady(`${url}/openapi.json`);
  } catch (e) {
    child.kill('SIGTERM');
    throw e;
  }
  return {
    child,
    url,
    close: () =>
      new Promise((resolve) => {
        if (child.exitCode !== null) return resolve();
        child.once('exit', () => resolve());
        child.kill('SIGTERM');
      }),
  };
}
