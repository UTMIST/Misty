export const REPOSITORY = 'UTMIST/Misty';
const SERVICE_ORDER = [
  'team-tracking',
  'llm',
  'connectors',
  'verification',
  'documentation-system',
  'meeting',
  'discord-bot',
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

export function validateEnvironment(environment) {
  if (environment.name !== 'dev' || environment.isEphemeral) {
    throw new Error('Only the persistent dev environment may be changed.');
  }
  if (nodes(environment.deploymentTriggers).length) {
    throw new Error('Disable automatic GitHub deployments in dev before selecting a PR.');
  }
  const services = nodes(environment.serviceInstances);
  if (services.some((service) => !SERVICE_ORDER.includes(service.serviceName))) {
    throw new Error('dev contains an unknown service; review the preview deployment list.');
  }
  for (const name of SERVICE_ORDER.filter((name) => name !== 'connectors')) {
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
  return [...services].sort(
    (a, b) => SERVICE_ORDER.indexOf(a.serviceName) - SERVICE_ORDER.indexOf(b.serviceName),
  );
}

async function waitFor(api, id, stopped, { sleep, now, timeoutMs, log }) {
  const deadline = now() + timeoutMs;
  let lastState;
  while (now() < deadline) {
    const { deployment } = await api(DEPLOYMENT_QUERY, { id });
    const state = `${deployment.status}${stopped ? ` (deploymentStopped: ${deployment.deploymentStopped})` : ''}`;
    if (state !== lastState) {
      log(`Deployment ${id}: ${state}.`);
      lastState = state;
    }
    if (stopped) {
      if (deployment.status === 'REMOVED' && deployment.deploymentStopped === true) return;
      // SUCCESS/SLEEPING can briefly persist after Railway accepts removal.
      // Other settled states cannot confirm the requested safe shutdown.
      if (
        FINISHED.has(deployment.status) &&
        deployment.status !== 'SUCCESS' &&
        deployment.status !== 'SLEEPING'
      ) {
        throw new Error(
          `Deployment ${id}: ${state}. Termination was not confirmed; inspect it before retrying. No new deployments were started.`,
        );
      }
    } else {
      if (deployment.status === 'SUCCESS') return;
      if (FINISHED.has(deployment.status)) {
        throw new Error(
          `Deployment ${id}: ${deployment.status}. The preview switch did not finish.`,
        );
      }
    }
    await sleep(3000);
  }
  throw new Error(
    `Timed out waiting for deployment ${id}: ${lastState}. Inspect it before retrying.`,
  );
}

// The caller supplies CLI/API access so the deployment sequence is tested
// offline. No credentials are copied or read by this command.
export async function switchPreview({
  projectId,
  pr,
  api,
  confirm,
  log = console.log,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = Date.now,
  timeoutMs = 15 * 60_000,
}) {
  validatePullRequest(pr);
  const { project } = await api(PROJECT_QUERY, { id: projectId });
  const target = nodes(project.environments).find((env) => env.name === 'dev');
  if (!target || target.isEphemeral || target.id === project.baseEnvironmentId) {
    throw new Error(
      'Create a persistent dev environment outside the PR base. See docs/pr-previews.md.',
    );
  }
  const { environment } = await api(ENVIRONMENT_QUERY, { id: target.id });
  const services = validateEnvironment(environment);
  const before = services.map((service) => [service.serviceId, service.latestDeployment?.id]);
  const plan = `PR #${pr.number} at ${pr.headRefOid}\nEnvironment: dev (${target.id})\nServices: ${services.map((service) => service.serviceName).join(', ')}`;
  log(plan);
  if (!(await confirm())) return { changed: false };

  const refreshed = validateEnvironment(
    (await api(ENVIRONMENT_QUERY, { id: target.id })).environment,
  );
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
  for (const deployment of bot.activeDeployments) {
    log(`Disconnecting previous preview (${deployment.id})…`);
    const removed = await api(REMOVE, { id: deployment.id });
    if (!removed.deploymentRemove) throw new Error('Railway did not accept the stop request.');
    await waitFor(api, deployment.id, true, waitOptions);
  }

  const completed = new Map();
  for (const service of services) {
    // Refuse to proceed if another operator/deployment has replaced a service
    // while this switch was in progress. A volume additionally makes the bot
    // singleton across Railway rollouts, including restarts outside this CLI.
    const current = (await api(ENVIRONMENT_QUERY, { id: target.id })).environment;
    const instances = validateEnvironment(current);
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
    await waitFor(api, id, false, waitOptions);
    completed.set(service.serviceId, id);
    log(`${service.serviceName}: ready (${id}).`);
  }
  const final = validateEnvironment((await api(ENVIRONMENT_QUERY, { id: target.id })).environment);
  const owners = final.find((service) => service.serviceName === 'discord-bot').activeDeployments;
  if (
    final.some((service) => service.latestDeployment?.id !== completed.get(service.serviceId)) ||
    owners.length !== 1 ||
    owners[0].id !== completed.get(bot.serviceId)
  ) {
    throw new Error('Another deployment changed the preview before verification completed.');
  }
  return { changed: true, environmentId: target.id, commitSha: pr.headRefOid };
}
