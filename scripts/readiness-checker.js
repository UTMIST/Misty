import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const DEFAULT_FAILURE_THRESHOLD = 2;
const DEFAULT_POLL_INTERVAL_SECONDS = 300;
const DEFAULT_TIMEOUT_MS = 10_000;
const DEFAULT_NOTIFICATION_TIMEOUT_MS = 10_000;

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
    notificationTimeoutMs: positiveInteger(
      env.READINESS_NOTIFICATION_TIMEOUT_MS,
      'READINESS_NOTIFICATION_TIMEOUT_MS',
      DEFAULT_NOTIFICATION_TIMEOUT_MS,
    ),
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
    // Only the status is needed; release any unfinished response body.
    controller.abort();
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

async function deliverNotification(sendNotification, alert, timeoutMs) {
  const controller = new AbortController();
  let timeout;
  try {
    const delivery = sendNotification(alert.content, { signal: controller.signal });
    const timedOut = new Promise((resolve) => {
      timeout = setTimeout(() => {
        controller.abort();
        resolve(false);
      }, timeoutMs);
    });
    return (await Promise.race([delivery, timedOut])) !== false;
  } catch {
    return false;
  } finally {
    // Header-only responses can still leave a socket receiving an unused body.
    controller.abort();
    clearTimeout(timeout);
  }
}

export async function runChecks(
  config,
  {
    state = { services: {} },
    fetchImpl = fetch,
    sendNotification = async (content, { signal }) => {
      if (!config.webhook) return false;
      const response = await fetchImpl(config.webhook, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
        signal,
      });
      if (!response.ok) throw new Error(`Discord webhook returned HTTP ${response.status}`);
      return true;
    },
    now = () => new Date(),
    notificationTimeoutMs = config.notificationTimeoutMs ?? DEFAULT_NOTIFICATION_TIMEOUT_MS,
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
      if (previous.pendingNotification?.type === 'recovery') {
        if (await deliverNotification(sendNotification, previous.pendingNotification, notificationTimeoutMs)) {
          notifications.push(previous.pendingNotification);
          delete current.pendingNotification;
          current.alertedDown = false;
        }
      } else if (previous.alertedDown) {
        const alert = notification(service, current, result, timestamp);
        if (await deliverNotification(sendNotification, alert, notificationTimeoutMs)) {
          notifications.push(alert);
          current.alertedDown = false;
          delete current.pendingNotification;
        } else {
          current.pendingNotification = alert;
        }
      } else {
        delete current.pendingNotification;
      }
      current.consecutiveFailures = 0;
      if (!current.pendingNotification) {
        current.alertedDown = false;
      }
    } else {
      current.consecutiveFailures = previous.consecutiveFailures + 1;
      if (previous.pendingNotification?.type === 'recovery') {
        delete current.pendingNotification;
        // The delivered outage remains open until a recovery is delivered.
        // Discard only the stale message, not the receipt of the down alert.
      } else if (previous.pendingNotification) {
        if (await deliverNotification(sendNotification, previous.pendingNotification, notificationTimeoutMs)) {
          notifications.push(previous.pendingNotification);
          delete current.pendingNotification;
          current.alertedDown = true;
        }
      }
      if (
        current.consecutiveFailures >= config.failureThreshold &&
        !current.alertedDown &&
        !current.pendingNotification
      ) {
        const alert = notification(service, current, result, timestamp);
        if (await deliverNotification(sendNotification, alert, notificationTimeoutMs)) {
          notifications.push(alert);
          current.alertedDown = true;
        } else {
          current.pendingNotification = alert;
        }
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
