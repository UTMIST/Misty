import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import { runtimeMode, discordEnvironment } from '../src/runtimeMode.js';
import { buildHealthServer } from '../src/healthServer.js';

const DEV = {
  RAILWAY_ENVIRONMENT_NAME: 'dev',
  RAILWAY_ENVIRONMENT_ID: 'dev-environment',
  MISTY_PREVIEW_ENVIRONMENT_ID: 'dev-environment',
  DISCORD_TOKEN_DEV: 'dev-token',
  DISCORD_CLIENT_ID_DEV: 'dev-app',
  DISCORD_GUILD_ID_DEV: 'dev-guild',
  DISCORD_TOKEN: 'inherited-token',
  DISCORD_CLIENT_ID: 'inherited-app',
  DISCORD_GUILD_ID: 'inherited-guild',
  ENABLE_DISCORD: 'true',
  ENABLE_WEB: 'true',
};

test('local, production, and staging retain their normal runtime', () => {
  assert.equal(runtimeMode({}), 'normal');
  for (const name of ['production', 'staging']) {
    assert.equal(runtimeMode({ RAILWAY_ENVIRONMENT_NAME: name }), 'normal');
  }
});

test('PR copies and unknown Railway names stay idle despite inherited credentials and flags', () => {
  for (const name of [
    'pr-123',
    'Misty-pr-123',
    'misty-dev',
    'renamed-preview',
    'prod',
    'Production',
    '',
  ]) {
    assert.equal(
      runtimeMode({ ...DEV, RAILWAY_ENVIRONMENT_NAME: name, RAILWAY_ENVIRONMENT_ID: 'copy' }),
      'idle',
    );
  }
  assert.throws(() => runtimeMode({ RAILWAY_ENVIRONMENT_NAME: 'dev' }), /ENVIRONMENT_ID/);
  assert.throws(() => runtimeMode({ ...DEV, RAILWAY_ENVIRONMENT_ID: 'copy' }), /ENVIRONMENT_ID/);
});

test('only the exact persistent preview environment selects dev credentials', () => {
  assert.equal(runtimeMode(DEV), 'preview');
  const env = discordEnvironment(DEV);
  assert.equal(env.DISCORD_TOKEN, 'dev-token');
  assert.equal(env.DISCORD_CLIENT_ID, 'dev-app');
  assert.equal(env.DISCORD_GUILD_ID, 'dev-guild');
  assert.equal(DEV.DISCORD_TOKEN, 'inherited-token');
});

test('missing dev credentials never fall back to inherited staging credentials', () => {
  for (const key of ['DISCORD_TOKEN_DEV', 'DISCORD_CLIENT_ID_DEV', 'DISCORD_GUILD_ID_DEV']) {
    assert.throws(() => discordEnvironment({ ...DEV, [key]: '' }), new RegExp(key));
  }
  const normal = { ...DEV, RAILWAY_ENVIRONMENT_NAME: 'staging' };
  assert.equal(discordEnvironment(normal), normal);
});

test('disabled Discord is never ready, even without credentials or a client', async () => {
  const server = buildHealthServer(null);
  try {
    const response = await server.inject('/health/ready');
    assert.equal(response.statusCode, 503);
    assert.deepEqual(response.json(), {
      status: 'unrecognized Railway environment',
      discord: 'disabled',
    });
  } finally {
    await server.close();
  }
});

test('direct registration in idle environments fails before config validation or Discord requests', async () => {
  const script = fileURLToPath(new URL('../src/registerCommands.js', import.meta.url));
  for (const name of ['pr-123', 'Production', 'secondary']) {
    await assert.rejects(
      promisify(execFile)(process.execPath, [script], {
        env: {
          PATH: process.env.PATH,
          RAILWAY_ENVIRONMENT_ID: 'other-environment',
          RAILWAY_ENVIRONMENT_NAME: name,
        },
        timeout: 10_000,
      }),
      (error) => error.code === 2 && /Refusing Discord command registration/.test(error.stderr),
    );
  }
  const config = JSON.parse(await readFile(new URL('../railway.json', import.meta.url), 'utf8'));
  assert.equal(config.deploy.preDeployCommand, 'node src/registerCommands.js');
  const [command, ...args] = config.environments.pr.deploy.preDeployCommand.split(' ');
  assert.equal(command, 'node');
  const { stdout } = await promisify(execFile)(process.execPath, args, {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: { PATH: process.env.PATH, RAILWAY_ENVIRONMENT_NAME: 'pr-123' },
    timeout: 10_000,
  });
  assert.match(stdout, /Skipping Discord registration/);
});

function idleWebProcess(enableDiscord, scopes = ['dev:spoof']) {
  // Exercise the real entrypoint and web server. Never allow gateway login or
  // a real backend request, including if startup regresses.
  const code = `
    import { Client } from 'discord.js';
    Client.prototype.login = async () => { throw new Error('Unexpected gateway login'); };
    globalThis.fetch = async (url) => {
      if (url !== 'http://directory.test/api-keys/self') throw new Error('Unexpected backend request');
      return new Response(JSON.stringify({ scopes: ${JSON.stringify(scopes)} }));
    };
    await import('./src/index.js');
  `;
  return {
    args: ['--input-type=module', '-e', code],
    options: {
      cwd: fileURLToPath(new URL('..', import.meta.url)),
      env: {
        PATH: process.env.PATH,
        PORT: '0',
        WEB_PORT: '0',
        RAILWAY_ENVIRONMENT_ID: 'copy',
        RAILWAY_ENVIRONMENT_NAME: 'pr-123',
        ENABLE_DISCORD: enableDiscord,
        ENABLE_WEB: 'true',
        DISCORD_TOKEN: 'unused-test-token',
        DISCORD_CLIENT_ID: 'unused-test-id',
        ...Object.fromEntries(
          ['DIRECTORY', 'DOC', 'LLM', 'VERIFICATION'].flatMap((service) => [
            [`${service}_BASE_URL`, `http://${service.toLowerCase()}.test`],
            [`${service}_API_KEY`, 'unused-test-key'],
          ]),
        ),
      },
    },
  };
}

for (const enableDiscord of ['false', 'true']) {
  test(`an idle Railway environment starts its requested playground with ENABLE_DISCORD=${enableDiscord}`, async () => {
    const { args, options } = idleWebProcess(enableDiscord);
    const child = spawn(process.execPath, args, options);
    const closed = new Promise((resolve) => child.once('close', resolve));
    let stdout = '';
    let stderr = '';
    let timer;
    try {
      await new Promise((resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error(`Web startup timed out: ${stdout}\n${stderr}`)),
          10_000,
        );
        child.stdout.on('data', (data) => {
          stdout += data;
          if (stdout.includes('Web playground:')) resolve();
        });
        child.stderr.on('data', (data) => {
          stderr += data;
        });
        child.once('error', reject);
        child.once('exit', (code) =>
          reject(new Error(`Boot exited ${code}: ${stdout}\n${stderr}`)),
        );
      });
      assert.equal(stderr, '');
    } finally {
      clearTimeout(timer);
      child.kill();
      await closed;
    }
  });
}

test('the idle playground still refuses a directory key without dev:spoof', async () => {
  const { args, options } = idleWebProcess('false', []);
  await assert.rejects(
    promisify(execFile)(process.execPath, args, { ...options, timeout: 10_000 }),
    (error) => error.code === 2 && /dev:spoof/.test(error.stderr),
  );
});

test('preview boot refuses to connect without a volume', async () => {
  const script = fileURLToPath(new URL('../src/index.js', import.meta.url));
  await assert.rejects(
    promisify(execFile)(process.execPath, [script], {
      env: {
        PATH: process.env.PATH,
        RAILWAY_ENVIRONMENT_NAME: 'dev',
        RAILWAY_ENVIRONMENT_ID: 'dev',
        MISTY_PREVIEW_ENVIRONMENT_ID: 'dev',
      },
      timeout: 10_000,
    }),
    (error) => error.code === 1 && /requires a Railway volume/.test(error.stderr),
  );
});
