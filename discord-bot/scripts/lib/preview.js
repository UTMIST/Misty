export const REPOSITORY = 'UTMIST/Misty';
export const DEPLOYMENT_TIMEOUT_MS = 45 * 60_000;
// Known backend dependencies run first; new services follow, then the bot.
// Inventory comes from the selected commit, not this ordering preference.
const BACKEND_ORDER = [
  'team-tracking',
  'llm',
  'connectors',
  'verification',
  'documentation-system',
  'meeting',
];
// Settled Railway statuses. SUCCESS/SLEEPING can still own a process; a
// stopped deployment additionally needs Railway's termination confirmation.
const FINISHED = new Set([
  'SUCCESS',
  'SLEEPING',
  'FAILED',
  'CRASHED',
  'REMOVED',
  'SKIPPED',
  'NEEDS_APPROVAL',
]);

export const PROJECT_QUERY = `query PreviewProject($id: String!) {
  project(id: $id) {
    id baseEnvironmentId
    environments { edges { node { id name isEphemeral } } }
  }
}`;

export const ENVIRONMENT_QUERY = `query PreviewEnvironment($id: String!) {
  environment(id: $id) {
    id name isEphemeral
    deploymentTriggers { edges { node { id } } }
    volumeInstances { edges { node { serviceId isPendingDeletion } } }
    serviceInstances { edges { node {
      serviceId serviceName source { repo }
      latestDeployment { id status }
      activeDeployments { id status }
    } } }
  }
}`;

const DEPLOYMENT_QUERY = `query PreviewDeployment($id: String!) {
  deployment(id: $id) { id status deploymentStopped }
}`;
const BOT_DEPLOYMENTS_QUERY = `query PreviewBotDeployments($environmentId: String!, $serviceId: String!, $after: String) {
  deployments(input: { environmentId: $environmentId, serviceId: $serviceId, includeDeleted: true }, first: 100, after: $after) {
    edges { node { id status deploymentStopped } }
    pageInfo { hasNextPage endCursor }
  }
}`;
const REMOVE = `mutation StopPreview($id: String!) { deploymentRemove(id: $id) }`;
const DEPLOY = `mutation DeployPreview($environmentId: String!, $serviceId: String!, $commitSha: String!) {
  serviceInstanceDeployV2(environmentId: $environmentId, serviceId: $serviceId, commitSha: $commitSha)
}`;

function nodes(connection) {
  return connection.edges.map(({ node }) => node);
}

export function validatePullRequest(pr) {
  if (
    pr.state !== 'OPEN' ||
    pr.baseRefName !== 'staging' ||
    pr.isCrossRepository ||
    !/^[a-f0-9]{40}$/.test(pr.headRefOid)
  ) {
    throw new Error('Choose an open PR from UTMIST/Misty targeting staging.');
  }
}

export function previewServices(source) {
  if (source.truncated || !Array.isArray(source.tree)) {
    throw new Error('GitHub returned an incomplete source tree; cannot validate this preview.');
  }
  if (
    !source.tree.some(
      (entry) => entry.path === 'discord-bot/src/runtimeMode.js' && entry.type === 'blob',
    )
  ) {
    throw new Error(
      'This PR lacks preview runtime support. Update its branch from staging before selecting it.',
    );
  }
  // The label-consistency check enforces a zone for every services/* directory.
  // Read the same inventory at the PR SHA so new backends cannot be omitted.
  return [
    ...source.tree
      .filter((entry) => entry.type === 'tree' && /^services\/[^/]+$/.test(entry.path))
      .map((entry) => entry.path.slice('services/'.length)),
    'discord-bot',
  ];
}

export function validateEnvironment(environment, expectedServices) {
  if (environment.name !== 'dev' || environment.isEphemeral) {
    throw new Error('Only the persistent dev environment may be changed.');
  }
  if (nodes(environment.deploymentTriggers).length) {
    throw new Error('Disable automatic GitHub deployments in dev before selecting a PR.');
  }
  const services = nodes(environment.serviceInstances);
  const extra = services.find((service) => !expectedServices.includes(service.serviceName));
  if (extra) {
    throw new Error(
      `dev contains ${extra.serviceName}, which is absent from this PR's service directories.`,
    );
  }
  for (const name of expectedServices.filter((name) => name !== 'connectors')) {
    if (!services.some((service) => service.serviceName === name)) {
      throw new Error(`dev is missing ${name}. See docs/pr-previews.md for setup.`);
    }
  }
  for (const service of services) {
    if (service.source?.repo !== REPOSITORY) {
      throw new Error(`${service.serviceName} must have ${REPOSITORY} as its source repository.`);
    }
    if (service.latestDeployment?.status === 'NEEDS_APPROVAL') {
      throw new Error(
        `${service.serviceName} has a deployment awaiting approval; approve or cancel it before switching.`,
      );
    }
    if (service.latestDeployment && !FINISHED.has(service.latestDeployment.status)) {
      throw new Error(
        `${service.serviceName} has an unfinished deployment; wait before switching.`,
      );
    }
  }
  const bot = services.find((service) => service.serviceName === 'discord-bot');
  if (
    !nodes(environment.volumeInstances).some(
      (volume) => volume.serviceId === bot.serviceId && !volume.isPendingDeletion,
    )
  ) {
    throw new Error('Attach a volume to the dev bot to prevent overlapping gateway sessions.');
  }
  const priority = (name) => {
    if (name === 'discord-bot') return BACKEND_ORDER.length + 1;
    const index = BACKEND_ORDER.indexOf(name);
    return index === -1 ? BACKEND_ORDER.length : index;
  };
  return [...services].sort(
    (a, b) =>
      priority(a.serviceName) - priority(b.serviceName) ||
      a.serviceName.localeCompare(b.serviceName),
  );
}

async function waitFor(api, id, stopped, { sleep, now, timeoutMs, log, allowSleeping = false }) {
  const deadline = now() + timeoutMs;
  let lastState;
  let intervalMs = 3000;
  while (now() < deadline) {
    const { deployment } = await api(DEPLOYMENT_QUERY, { id });
    const state = `${deployment.status}${stopped ? ` (deploymentStopped: ${deployment.deploymentStopped})` : ''}`;
    if (state !== lastState) {
      log(`Deployment ${id}: ${state}.`);
      lastState = state;
      intervalMs = 3000;
    } else if (
      !stopped &&
      ['BUILDING', 'QUEUED', 'INITIALIZING', 'WAITING'].includes(deployment.status)
    ) {
      intervalMs = Math.min(intervalMs * 2, 30_000);
    } else {
      intervalMs = 3000;
    }
    if (stopped) {
      if (deployment.status === 'REMOVED' && deployment.deploymentStopped === true)
        return deployment.status;
      // Removal and instance shutdown are separate signals. REMOVED may
      // precede deploymentStopped; keep polling until both confirm shutdown.
      // Any status (including CRASHED/FAILED) can lag an accepted stop request.
    } else {
      if (deployment.status === 'SUCCESS' || (allowSleeping && deployment.status === 'SLEEPING'))
        return deployment.status;
      if (FINISHED.has(deployment.status)) {
        throw new Error(
          `Deployment ${id}: ${deployment.status}. The preview switch did not finish.`,
        );
      }
    }
    const remaining = deadline - now();
    if (remaining > 0) await sleep(Math.min(intervalMs, remaining));
  }
  throw new Error(
    `Timed out waiting for deployment ${id}: ${lastState}. Inspect it before retrying.`,
  );
}

async function readBotDeployments(api, environmentId, bot, previouslyObserved) {
  const history = new Map();
  let after = null;
  do {
    const { deployments } = await api(BOT_DEPLOYMENTS_QUERY, {
      environmentId,
      serviceId: bot.serviceId,
      after,
    });
    for (const deployment of nodes(deployments)) history.set(deployment.id, deployment);
    if (!deployments.pageInfo.hasNextPage) break;
    const cursor = deployments.pageInfo.endCursor;
    if (!cursor || cursor === after)
      throw new Error('Incomplete bot deployment history; refusing to switch.');
    after = cursor;
  } while (after);
  // Preserve IDs seen before/during confirmation even if a concurrent removal
  // has already made them disappear from the history or active deployment list.
  for (const instance of [previouslyObserved, bot]) {
    for (const observed of [...instance.activeDeployments, instance.latestDeployment].filter(
      Boolean,
    )) {
      if (!history.has(observed.id)) {
        const { deployment } = await api(DEPLOYMENT_QUERY, { id: observed.id });
        history.set(observed.id, deployment);
      }
    }
  }
  for (const deployment of history.values()) {
    if (
      (!FINISHED.has(deployment.status) && deployment.status !== 'REMOVING') ||
      deployment.status === 'NEEDS_APPROVAL'
    ) {
      throw new Error(
        `Bot deployment ${deployment.id} is ${deployment.status}; resolve it before switching.`,
      );
    }
  }
  return [...history.values()].filter(
    (deployment) =>
      deployment.deploymentStopped !== true ||
      ['SUCCESS', 'SLEEPING', 'CRASHED'].includes(deployment.status),
  );
}

// The caller supplies CLI/API access so the deployment sequence is tested
// offline. No credentials are copied or read by this command.
export async function switchPreview({
  projectId,
  pr,
  readSource,
  api,
  confirm,
  log = console.log,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = Date.now,
  timeoutMs = DEPLOYMENT_TIMEOUT_MS,
  stopTimeoutMs = 15 * 60_000,
}) {
  validatePullRequest(pr);
  const expectedServices = previewServices(await readSource(pr.headRefOid));
  const { project } = await api(PROJECT_QUERY, { id: projectId });
  const target = nodes(project.environments).find((env) => env.name === 'dev');
  if (!target || target.isEphemeral || target.id === project.baseEnvironmentId) {
    throw new Error(
      'Create a persistent dev environment outside the PR base. See docs/pr-previews.md.',
    );
  }
  const readServices = async () =>
    validateEnvironment(
      (await api(ENVIRONMENT_QUERY, { id: target.id })).environment,
      expectedServices,
    );
  const services = await readServices();
  const before = services.map((service) => [service.serviceId, service.latestDeployment?.id]);
  const plan = `PR #${pr.number} at ${pr.headRefOid}\nEnvironment: dev (${target.id})\nServices: ${services.map((service) => service.serviceName).join(', ')}`;
  log(plan);
  if (
    expectedServices.includes('connectors') &&
    !services.some((service) => service.serviceName === 'connectors')
  ) {
    log('Optional connectors service is absent from dev and will not be tested.');
  }
  if (!(await confirm())) return { changed: false };

  const refreshed = await readServices();
  if (
    JSON.stringify(before) !==
    JSON.stringify(refreshed.map((service) => [service.serviceId, service.latestDeployment?.id]))
  ) {
    throw new Error('dev changed while awaiting confirmation. Run the command again.');
  }

  // Stop the bot before any backend migration or meeting-service restart. Wait
  // for actual removal, not merely a successful request to remove it.
  const bot = refreshed.find((service) => service.serviceName === 'discord-bot');
  const waitOptions = { sleep, now, timeoutMs, log };
  const stopDeadline = now() + stopTimeoutMs;
  const oldDeployments = await readBotDeployments(
    api,
    target.id,
    bot,
    services.find((service) => service.serviceName === 'discord-bot'),
  );
  for (const deployment of oldDeployments) {
    const remaining = stopDeadline - now();
    if (remaining <= 0)
      throw new Error(
        'Timed out confirming previous bot shutdown. No new deployments were started.',
      );
    if (!['REMOVED', 'REMOVING'].includes(deployment.status)) {
      log(`Disconnecting previous preview (${deployment.id})…`);
      const removed = await api(REMOVE, { id: deployment.id });
      if (!removed.deploymentRemove) throw new Error('Railway did not accept the stop request.');
    }
    log(`Confirming previous preview shutdown (${deployment.id})…`);
    await waitFor(api, deployment.id, true, { ...waitOptions, timeoutMs: stopDeadline - now() });
  }

  const completed = new Map();
  for (const service of services) {
    // Refuse to proceed if another operator/deployment has replaced a service
    // while this switch was in progress. A volume additionally makes the bot
    // singleton across Railway rollouts, including restarts outside this CLI.
    const instances = await readServices();
    const currentBot = instances.find((instance) => instance.serviceName === 'discord-bot');
    if (currentBot.activeDeployments.length) {
      throw new Error('Another bot deployment became active during the switch. Stopping here.');
    }
    for (const instance of instances) {
      const expected = completed.get(instance.serviceId);
      if (expected && instance.latestDeployment?.id !== expected) {
        throw new Error('Another deployment changed dev during the switch. Stopping here.');
      }
    }
    log(`Deploying ${service.serviceName}…`);
    const result = await api(DEPLOY, {
      environmentId: target.id,
      serviceId: service.serviceId,
      commitSha: pr.headRefOid,
    });
    const id = result.serviceInstanceDeployV2;
    if (!id) throw new Error('Railway returned no deployment ID. Inspect dev before retrying.');
    const status = await waitFor(api, id, false, {
      ...waitOptions,
      allowSleeping: service.serviceName !== 'discord-bot',
    });
    completed.set(service.serviceId, id);
    log(`${service.serviceName}: ${status.toLowerCase()} (${id}).`);
  }
  const final = await readServices();
  const owners = final.find((service) => service.serviceName === 'discord-bot').activeDeployments;
  if (
    final.some((service) => service.latestDeployment?.id !== completed.get(service.serviceId)) ||
    owners.length !== 1 ||
    owners[0].id !== completed.get(bot.serviceId) ||
    owners[0].status !== 'SUCCESS'
  ) {
    throw new Error('Another deployment changed the preview before verification completed.');
  }
  return { changed: true, environmentId: target.id, commitSha: pr.headRefOid };
}
