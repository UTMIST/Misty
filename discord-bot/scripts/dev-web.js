import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { snapshotDb } from './lib/snapshotDb.js';
import { spawnTeamTracking } from './lib/spawnTeamTracking.js';
import { issueDevSpoofKey } from './lib/issueDevSpoofKey.js';
import { seedDefaultPersonas } from './lib/seedDefaultPersonas.js';
import { commands } from '../src/commands/index.js';
import { createAppContext } from '../src/context.js';
import { ensureDevSpoofScope } from '../src/startupGuard.js';
import { startWebServer } from '../src/web/server.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const teamTrackingDir = path.resolve(__dirname, '../../services/team-tracking');
const SCRATCH_DB = 'team_tracking_playground';
const MAIN_DB = 'team_tracking';
const DB_USER = 'team_tracking';
const DB_PASSWORD = 'dev_password';
const DB_HOST = 'localhost';
const DB_PORT = '5433';
const SCRATCH_PORT = 8001;
const WEB_PORT = Number(process.env.WEB_PORT || 3001);

function scratchDatabaseUrl() {
  return `postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${SCRATCH_DB}`;
}

async function ensurePostgresUp() {
  return new Promise((resolve, reject) => {
    const child = spawn('docker', ['compose', 'up', '-d', 'postgres'], {
      cwd: teamTrackingDir,
      stdio: 'inherit',
    });
    child.on('exit', (code) =>
      code === 0 ? resolve() : reject(new Error(`docker compose up failed: ${code}`)),
    );
  });
}

// The scratch team-tracking process keeps a SQLAlchemy connection pool open
// against the scratch DB. Postgres refuses DROP/re-clone while any session
// (even idle) is attached, so force-disconnect before every snapshot.
async function terminateScratchConnections() {
  return new Promise((resolve) => {
    const child = spawn(
      'docker',
      [
        'compose',
        'exec',
        '-T',
        'postgres',
        'psql',
        '-U',
        DB_USER,
        '-d',
        'postgres',
        '-c',
        `SELECT pg_terminate_backend(pid) FROM pg_stat_activity ` +
          `WHERE datname = '${SCRATCH_DB}' AND pid <> pg_backend_pid();`,
      ],
      { cwd: teamTrackingDir, stdio: 'inherit' },
    );
    child.on('exit', () => resolve()); // best-effort — fine if the DB doesn't exist yet
  });
}

async function countPeople() {
  return new Promise((resolve) => {
    const child = spawn(
      'docker',
      [
        'compose',
        'exec',
        '-T',
        'postgres',
        'psql',
        '-U',
        DB_USER,
        '-d',
        SCRATCH_DB,
        '-t',
        '-A',
        '-c',
        'SELECT count(*) FROM people;',
      ],
      { cwd: teamTrackingDir, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    let out = '';
    child.stdout.on('data', (d) => {
      out += d.toString();
    });
    child.on('exit', () => resolve(parseInt(out.trim(), 10) || 0));
  });
}

async function dropScratchDb() {
  return new Promise((resolve) => {
    const child = spawn(
      'docker',
      [
        'compose',
        'exec',
        '-T',
        'postgres',
        'psql',
        '-U',
        DB_USER,
        '-d',
        'postgres',
        '-c',
        `DROP DATABASE IF EXISTS ${SCRATCH_DB};`,
      ],
      { cwd: teamTrackingDir, stdio: 'inherit' },
    );
    child.on('exit', () => resolve()); // best-effort
  });
}

let scratch = null;
let webServer = null;
let shuttingDown = false;

async function shutdown(sig) {
  if (shuttingDown) {
    console.error(`▶ ${sig} received during shutdown — forcing exit`);
    process.exit(1);
  }
  shuttingDown = true;
  console.log(`\n▶ ${sig} received — shutting down…`);
  const watchdog = setTimeout(() => {
    console.error('▶ shutdown watchdog fired — forcing exit');
    process.kill(process.pid, 'SIGKILL');
  }, 5000);
  watchdog.unref();
  if (webServer) {
    try {
      await webServer.close();
    } catch (e) {
      console.error(e);
    }
  }
  if (scratch) {
    try {
      await scratch.close();
    } catch (e) {
      console.error(e);
    }
  }
  try {
    await dropScratchDb();
  } catch (e) {
    console.error(e);
  }
  clearTimeout(watchdog);
  process.exit(0);
}

async function main() {
  console.log('▶ ensuring postgres is up…');
  await ensurePostgresUp();

  console.log(`▶ cloning ${MAIN_DB} → ${SCRATCH_DB}…`);
  await snapshotDb({ source: MAIN_DB, target: SCRATCH_DB, dbUser: DB_USER, teamTrackingDir });
  console.log(`  scratch has ${await countPeople()} people from main`);

  console.log(`▶ spawning scratch team-tracking on port ${SCRATCH_PORT}…`);
  scratch = await spawnTeamTracking({
    port: SCRATCH_PORT,
    databaseUrl: scratchDatabaseUrl(),
    teamTrackingDir,
  });

  console.log('▶ issuing dev:spoof-scoped API key against scratch…');
  const key = await issueDevSpoofKey({
    teamTrackingDir,
    databaseUrl: scratchDatabaseUrl(),
    name: 'discord-bot-playground',
  });
  console.log(`  key prefix: ${key.slice(0, 12)}…`);

  console.log('▶ seeding default personas into scratch…');
  await seedDefaultPersonas({ baseUrl: scratch.url, apiKey: key });

  const config = {
    directoryBaseUrl: scratch.url,
    directoryApiKey: key,
    // Discord fields not used in web-only mode; safe placeholders.
    discordToken: null,
    discordClientId: null,
    discordGuildId: undefined,
  };
  const appContext = createAppContext(config);

  await ensureDevSpoofScope(appContext);

  const onReset = async () => {
    console.log(`▶ re-cloning ${MAIN_DB} → ${SCRATCH_DB}…`);
    await terminateScratchConnections();
    await snapshotDb({ source: MAIN_DB, target: SCRATCH_DB, dbUser: DB_USER, teamTrackingDir });
    console.log(`  scratch has ${await countPeople()} people from main`);
    // Re-cloning replaces the scratch api_keys table with main's, which wipes
    // the key issued after the previous clone. Reissue and rewire appContext
    // in place so already-dispatched route handlers (which read appContext.*
    // fresh on every request) pick up the new key without a server restart.
    console.log('▶ re-issuing dev:spoof-scoped API key against scratch…');
    const newKey = await issueDevSpoofKey({
      teamTrackingDir,
      databaseUrl: scratchDatabaseUrl(),
      name: 'discord-bot-playground',
    });
    console.log('▶ re-seeding default personas into scratch…');
    await seedDefaultPersonas({ baseUrl: scratch.url, apiKey: newKey });
    Object.assign(appContext, createAppContext({ ...config, directoryApiKey: newKey }));
  };

  console.log(`▶ starting web playground on port ${WEB_PORT}…`);
  webServer = await startWebServer({ commands, appContext, port: WEB_PORT, onReset });

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch(async (err) => {
  console.error('▶ fatal:', err);
  try {
    if (scratch) await scratch.close();
    await dropScratchDb();
  } catch (cleanupErr) {
    console.error('▶ cleanup failed:', cleanupErr);
  }
  process.exit(1);
});
