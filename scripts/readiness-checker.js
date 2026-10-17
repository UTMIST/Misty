import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const DEFAULT_FAILURE_THRESHOLD = 2;
const DEFAULT_POLL_INTERVAL_SECONDS = 300;
const DEFAULT_TIMEOUT_MS = 10_000;

function positiveInteger(value, name, fallback) {
  if (value === undefined || value === '') return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

export function loadConfig(env = process.env) {
  let endpoints;
  try {
    endpoints = JSON.parse(env.READINESS_ENDPOINTS || '{}');
  } catch (error) {
    throw new Error(`READINESS_ENDPOINTS must be valid JSON: ${error.message}`);
  }
  if (!endpoints || Array.isArray(endpoints) || typeof endpoints !== 'object') {
    throw new Error('READINESS_ENDPOINTS must be a JSON object of service names to URLs');
  }
  for (const [service, url] of Object.entries(endpoints)) {
    if (!service || typeof url !== 'string' || !url.startsWith('https://')) {
      throw new Error(`READINESS_ENDPOINTS.${service || '<empty>'} must be an HTTPS URL`);
    }
  }

  return {
    endpoints,
    failureThreshold: positiveInteger(
      env.READINESS_FAILURE_THRESHOLD,
      'READINESS_FAILURE_THRESHOLD',
      DEFAULT_FAILURE_THRESHOLD,
    ),
    pollIntervalSeconds: positiveInteger(
      env.READINESS_POLL_INTERVAL_SECONDS,
      'READINESS_POLL_INTERVAL_SECONDS',
      DEFAULT_POLL_INTERVAL_SECONDS,
    ),
    timeoutMs: positiveInteger(env.READINESS_TIMEOUT_MS, 'READINESS_TIMEOUT_MS', DEFAULT_TIMEOUT_MS),
    stateFile: env.READINESS_STATE_FILE || '.readiness-state.json',
    webhook: env.DISCORD_READINESS_WEBHOOK || undefined,
  };
}

export async function loadState(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return { services: {} };
    throw new Error(`Could not read readiness state: ${error.message}`);
  }
}

export async function saveState(path, state) {
  await writeFile(path, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
}

export function shouldPoll(state, now, intervalSeconds) {
  const lastCheckedAt = Object.values(state.services || {})
    .map((service) => service.lastCheckedAt)
    .filter(Boolean)
    .sort()
    .at(-1);
  if (!lastCheckedAt) return true;
  return now.getTime() - new Date(lastCheckedAt).getTime() >= intervalSeconds * 1000;
}

async function checkEndpoint(url, timeoutMs, fetchImpl = fetch) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    return { ok: response.ok, status: response.status };
  } catch {
    return { ok: false, status: 'offline' };
  } finally {
    clearTimeout(timeout);
  }
}

function notification(service, state, result, timestamp) {
  if (state.alertedDown) {
    return {
      type: 'recovery',
      content: `🟢 **Service recovered** — \`${service}\` is ready again (status ${result.status}) at ${timestamp}.`,
    };
  }
  return {
    type: 'down',
    content: `🔴 **Service outage** — \`${service}\` readiness failed ${state.consecutiveFailures} consecutive checks (status ${result.status}) at ${timestamp}.`,
  };
}

export async function runChecks(
  config,
  {
    state = { services: {} },
    fetchImpl = fetch,
    sendNotification = async (content) => {
      if (!config.webhook) return;
      const response = await fetchImpl(config.webhook, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      if (!response.ok) throw new Error(`Discord webhook returned HTTP ${response.status}`);
    },
    now = () => new Date(),
  } = {},
) {
  const timestamp = now().toISOString();
  const notifications = [];
  const nextState = { services: {} };

  for (const [service, url] of Object.entries(config.endpoints)) {
    const result = await checkEndpoint(url, config.timeoutMs, fetchImpl);
    const previous = state.services[service] || {
      consecutiveFailures: 0,
      alertedDown: false,
    };
    const current = { ...previous, lastCheckedAt: timestamp, lastStatus: result.status };

    if (result.ok) {
      if (previous.alertedDown) {
        const alert = notification(service, current, result, timestamp);
        await sendNotification(alert.content);
        notifications.push(alert);
      }
      current.consecutiveFailures = 0;
      current.alertedDown = false;
    } else {
      current.consecutiveFailures = previous.consecutiveFailures + 1;
      if (
        current.consecutiveFailures >= config.failureThreshold &&
        !previous.alertedDown
      ) {
        const alert = notification(service, current, result, timestamp);
        await sendNotification(alert.content);
        notifications.push(alert);
        current.alertedDown = true;
      }
    }
    nextState.services[service] = current;
  }

  return { state: nextState, notifications };
}

async function main() {
  const config = loadConfig();
  if (Object.keys(config.endpoints).length === 0) {
    await saveState(config.stateFile, { services: {} });
    console.warn('No readiness endpoints configured; set READINESS_ENDPOINTS to enable checks.');
    return;
  }
  const state = await loadState(config.stateFile);
  const now = new Date();
  if (!shouldPoll(state, now, config.pollIntervalSeconds)) {
    console.log(`Skipping readiness check; next check is due after ${config.pollIntervalSeconds}s.`);
    return;
  }
  const result = await runChecks(config, { state, now: () => now });
  await saveState(config.stateFile, result.state);
  console.log(
    `Checked ${Object.keys(config.endpoints).length} service(s) at ${new Date().toISOString()}; ` +
      `${result.notifications.length} notification(s) sent.`,
  );
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
