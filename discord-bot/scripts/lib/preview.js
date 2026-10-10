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
const FINISHED = new Set(['SUCCESS', 'SLEEPING', 'FAILED', 'CRASHED', 'REMOVED', 'SKIPPED']);
const RUNNABLE = new Set(['SUCCESS', 'SLEEPING', 'CRASHED']);
const STOPPED_INSTANCES = new Set(['CRASHED', 'EXITED', 'REMOVED', 'SKIPPED', 'STOPPED']);

export const PROJECT_TOKEN_QUERY = `query PreviewProjectToken {
  projectToken {
    projectId environmentId
    project { baseEnvironmentId }
  }
}`;

export const PROJECT_QUERY = `query PreviewProject($id: String!) {
  project(id: $id) {
    id baseEnvironmentId
    environments { edges { node { id name isEphemeral } } }
  }
}`;

// activeDeployments is [Deployment!]! in Railway's schema; a service that has
// never deployed returns []. GraphQL errors are rejected by railwayApi.
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
    edges { node { id status deploymentStopped instances { status } } }
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

function requireApproved(deployment, label) {
  if (deployment?.status === 'NEEDS_APPROVAL') {
    throw new Error(
      `${label} has a deployment awaiting approval (NEEDS_APPROVAL); approve or cancel it before switching.`,
    );
  }
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
    requireApproved(service.latestDeployment, service.serviceName);
    // The stop phase can finish an operator's removal of the bot. Backend
    // removals must still settle before we replace any services.
    const stoppingBot =
      service.serviceName === 'discord-bot' && service.latestDeployment?.status === 'REMOVING';
    if (
      service.latestDeployment &&
      !FINISHED.has(service.latestDeployment.status) &&
      !stoppingBot
    ) {
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
  let lastState = 'not observed before the deadline';
  let intervalMs = 3000;
  while (now() < deadline) {
    const { deployment } = await api(DEPLOYMENT_QUERY, { id });
    const state = `${deployment.status}${stopped ? ` (deploymentStopped: ${deployment.deploymentStopped})` : ''}`;
    intervalMs =
      state === lastState &&
      !stopped &&
      ['BUILDING', 'QUEUED', 'INITIALIZING', 'WAITING'].includes(deployment.status)
        ? Math.min(intervalMs * 2, 30_000)
        : 3000;
    if (state !== lastState) {
      log(`Deployment ${id}: ${state}.`);
      lastState = state;
    }
    if (stopped) {
      if (deployment.status === 'REMOVED' && deployment.deploymentStopped === true)
        return deployment.status;
      // Railway defines deploymentStopped as all instances having stopped,
      // including superseded deployments, not just explicit stop requests.
      // Removal and instance shutdown are separate signals. REMOVED may
      // precede deploymentStopped; keep polling until both confirm shutdown.
      // Any status (including CRASHED/FAILED) can lag an accepted stop request.
    } else {
      requireApproved(deployment, `Deployment ${id}`);
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
  const observedGateways = new Set();
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
    for (const observed of instance.activeDeployments) observedGateways.add(observed.id);
    if (
      RUNNABLE.has(instance.latestDeployment?.status) ||
      instance.latestDeployment?.status === 'REMOVING'
    )
      observedGateways.add(instance.latestDeployment.id);
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
    requireApproved(deployment, `Bot deployment ${deployment.id}`);
    if (!FINISHED.has(deployment.status) && deployment.status !== 'REMOVING') {
      throw new Error(
        `Bot deployment ${deployment.id} is ${deployment.status}; resolve it before switching.`,
      );
    }
  }
  // A false stop flag on an old failed/skipped/removed build is not evidence
  // of a gateway. Keep only runnable/removing deployments, gateways observed
  // during this switch, and historical deployments with nonterminal instances.
  // Confirmed-stopped history needs no further removal. A current gateway is
  // different: even if between instances, it can still restart or wake.
  return [...history.values()].filter((deployment) => {
    if (deployment.deploymentStopped === true) {
      return observedGateways.has(deployment.id) && RUNNABLE.has(deployment.status);
    }
    return (
      RUNNABLE.has(deployment.status) ||
      deployment.status === 'REMOVING' ||
      observedGateways.has(deployment.id) ||
      deployment.instances?.some((instance) => !STOPPED_INSTANCES.has(instance.status))
    );
  });
}

// The caller supplies CLI/API access so the deployment sequence is tested
// offline. No credentials are copied or read by this command.
export async function switchPreview({
  projectId,
  projectToken = false,
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
  let targetId;
  let baseEnvironmentId;
  if (projectToken) {
    // RAILWAY_TOKEN is scoped to one environment. Resolve it without listing
    // other environments; project metadata retains the PR-base safety check.
    const { projectToken: scope } = await api(PROJECT_TOKEN_QUERY, {});
    if (scope.projectId !== projectId || !scope.environmentId || !scope.project) {
      throw new Error('RAILWAY_TOKEN must belong to the selected project and its dev environment.');
    }
    targetId = scope.environmentId;
    baseEnvironmentId = scope.project.baseEnvironmentId;
  } else {
    const { project } = await api(PROJECT_QUERY, { id: projectId });
    const target = nodes(project.environments).find(
      (env) => env.name === 'dev' && !env.isEphemeral,
    );
    targetId = target?.id;
    baseEnvironmentId = project.baseEnvironmentId;
  }
  if (!targetId || targetId === baseEnvironmentId) {
    throw new Error(
      'Create a persistent dev environment outside the PR base. See docs/pr-previews.md.',
    );
  }
  const readServices = async () =>
    validateEnvironment(
      (await api(ENVIRONMENT_QUERY, { id: targetId })).environment,
      expectedServices,
    );
  const services = await readServices();
  const before = services.map((service) => [service.serviceId, service.latestDeployment?.id]);
  const plan = `PR #${pr.number} at ${pr.headRefOid}\nEnvironment: dev (${targetId})\nServices: ${services.map((service) => service.serviceName).join(', ')}`;
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
  const remainingStopTime = () => {
    const remaining = stopDeadline - now();
    if (remaining <= 0)
      throw new Error(
        'Timed out confirming previous bot shutdown. No new deployments were started.',
      );
    return remaining;
  };
  const oldDeployments = await readBotDeployments(
    api,
    targetId,
    bot,
    services.find((service) => service.serviceName === 'discord-bot'),
  );
  for (const deployment of oldDeployments) {
    remainingStopTime();
    if (!['REMOVED', 'REMOVING'].includes(deployment.status)) {
      log(`Disconnecting previous preview (${deployment.id})…`);
      const removed = await api(REMOVE, { id: deployment.id });
      if (!removed.deploymentRemove) throw new Error('Railway did not accept the stop request.');
    }
    log(`Confirming previous preview shutdown (${deployment.id})…`);
    await waitFor(api, deployment.id, true, { ...waitOptions, timeoutMs: remainingStopTime() });
  }

  const expectedDeployments = new Map(before);
  for (const service of services) {
    // Refuse to proceed if another operator/deployment has replaced a service
    // while this switch was in progress. A volume additionally makes the bot
    // singleton across Railway rollouts, including restarts outside this CLI.
    const instances = await readServices();
    const currentBot = instances.find((instance) => instance.serviceName === 'discord-bot');
    if (currentBot.activeDeployments.length) {
      throw new Error('Another bot deployment became active during the switch. Stopping here.');
    }
    if (instances.length !== expectedDeployments.size) {
      throw new Error('The services in dev changed during the switch. Stopping here.');
    }
    for (const instance of instances) {
      if (
        !expectedDeployments.has(instance.serviceId) ||
        instance.latestDeployment?.id !== expectedDeployments.get(instance.serviceId)
      ) {
        throw new Error('Another deployment changed dev during the switch. Stopping here.');
      }
    }
    log(`Deploying ${service.serviceName}…`);
    const result = await api(DEPLOY, {
      environmentId: targetId,
      serviceId: service.serviceId,
      commitSha: pr.headRefOid,
    });
    const id = result.serviceInstanceDeployV2;
    if (!id) throw new Error('Railway returned no deployment ID. Inspect dev before retrying.');
    const status = await waitFor(api, id, false, {
      ...waitOptions,
      allowSleeping: service.serviceName !== 'discord-bot',
    });
    expectedDeployments.set(service.serviceId, id);
    log(`${service.serviceName}: ${status.toLowerCase()} (${id}).`);
  }
  const final = await readServices();
  const owners = final.find((service) => service.serviceName === 'discord-bot').activeDeployments;
  if (
    final.length !== expectedDeployments.size ||
    final.some(
      (service) => service.latestDeployment?.id !== expectedDeployments.get(service.serviceId),
    ) ||
    owners.length !== 1 ||
    owners[0].id !== expectedDeployments.get(bot.serviceId) ||
    owners[0].status !== 'SUCCESS'
  ) {
    throw new Error('Another deployment changed the preview before verification completed.');
  }
  return { changed: true, environmentId: targetId, commitSha: pr.headRefOid };
}
