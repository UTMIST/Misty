import { test } from 'node:test';
import assert from 'node:assert/strict';
import { switchPreview, validateEnvironment, validatePullRequest } from '../scripts/lib/preview.js';

const connection = (values) => ({ edges: values.map((node) => ({ node })) });
const PR = {
  number: 123,
  state: 'OPEN',
  baseRefName: 'staging',
  headRefOid: 'a'.repeat(40),
  isCrossRepository: false,
};

function fixture({
  failService,
  failureStatus = 'FAILED',
  initialStatus = 'SUCCESS',
  firstStopStatus = 'REMOVING',
  stopResult,
  stuckStop = false,
  otherDeployment = false,
} = {}) {
  const names = [
    'discord-bot',
    'meeting',
    'documentation-system',
    'verification',
    'llm',
    'team-tracking',
  ];
  const instances = names.map((name) => ({
    serviceId: name,
    serviceName: name,
    source: { repo: 'UTMIST/Misty' },
    latestDeployment: { id: `${name}-old`, status: initialStatus },
    activeDeployments: [{ id: `${name}-old`, status: initialStatus }],
  }));
  const environment = {
    id: 'dev',
    name: 'dev',
    isEphemeral: false,
    deploymentTriggers: connection([]),
    volumeInstances: connection([{ serviceId: 'discord-bot', isPendingDeletion: false }]),
    serviceInstances: connection(instances),
  };
  const project = {
    id: 'project',
    baseEnvironmentId: 'staging',
    environments: connection([{ id: 'dev', name: 'dev', isEphemeral: false }]),
  };
  const calls = [];
  const logs = [];
  let stopRequested = false;
  let stopReads = 0;
  let clock = 0;
  const api = async (query, variables) => {
    calls.push({ query, variables });
    if (query.includes('query PreviewProject')) return { project: structuredClone(project) };
    if (query.includes('query PreviewEnvironment')) {
      if (
        otherDeployment &&
        instances.find((s) => s.serviceId === 'team-tracking').latestDeployment.id.endsWith('-new')
      ) {
        instances.find((s) => s.serviceId === 'team-tracking').latestDeployment.id =
          'another-deploy';
      }
      return { environment: structuredClone(environment) };
    }
    if (query.includes('mutation StopPreview')) {
      assert.equal(variables.id, 'discord-bot-old');
      stopRequested = true;
      return { deploymentRemove: true };
    }
    if (query.includes('query PreviewDeployment')) {
      if (variables.id === 'discord-bot-old') {
        assert.equal(stopRequested, true);
        stopReads += 1;
        if (stuckStop || stopReads === 1) {
          return {
            deployment: { id: variables.id, status: firstStopStatus, deploymentStopped: false },
          };
        }
        if (stopResult) return { deployment: { id: variables.id, ...stopResult } };
        const bot = instances.find((s) => s.serviceId === 'discord-bot');
        bot.activeDeployments = [];
        bot.latestDeployment.status = 'REMOVED';
        return { deployment: { id: variables.id, status: 'REMOVED', deploymentStopped: true } };
      }
      const service = instances.find((s) => s.latestDeployment.id === variables.id);
      assert.ok(service);
      service.latestDeployment.status =
        service.serviceId === failService ? failureStatus : 'SUCCESS';
      service.activeDeployments = [service.latestDeployment];
      return { deployment: { ...service.latestDeployment, deploymentStopped: false } };
    }
    if (query.includes('mutation DeployPreview')) {
      assert.equal(
        instances.find((s) => s.serviceId === 'discord-bot').activeDeployments.length,
        0,
        'must wait until the previous bot has actually stopped',
      );
      assert.equal(variables.environmentId, 'dev');
      assert.equal(variables.commitSha, PR.headRefOid);
      const service = instances.find((s) => s.serviceId === variables.serviceId);
      service.latestDeployment = { id: `${service.serviceId}-new`, status: 'BUILDING' };
      return { serviceInstanceDeployV2: service.latestDeployment.id };
    }
    throw new Error(`Unexpected API query: ${query}`);
  };
  return {
    environment,
    project,
    calls,
    logs,
    options: {
      projectId: 'project',
      pr: PR,
      api,
      confirm: async () => true,
      log: (message) => logs.push(message),
      now: () => clock,
      sleep: async (ms) => {
        clock += ms;
      },
      timeoutMs: 10_000,
    },
  };
}

const mutations = (calls) => calls.filter(({ query }) => query.startsWith('mutation'));

test('switch waits for disconnect, pins all services to the PR commit, and deploys bot last', async () => {
  const { options, calls } = fixture();
  const result = await switchPreview(options);
  assert.equal(result.changed, true);
  const changes = mutations(calls);
  assert.match(changes[0].query, /StopPreview/);
  assert.deepEqual(
    changes.slice(1).map(({ variables }) => variables.serviceId),
    ['team-tracking', 'llm', 'verification', 'documentation-system', 'meeting', 'discord-bot'],
  );
});

test('plan or declined confirmation performs no mutation', async () => {
  const { options, calls } = fixture();
  const result = await switchPreview({ ...options, confirm: async () => false });
  assert.equal(result.changed, false);
  assert.deepEqual(mutations(calls), []);
});

test('a deployment changed during confirmation is not stopped or overwritten', async () => {
  const { options, environment, calls } = fixture();
  await assert.rejects(
    switchPreview({
      ...options,
      confirm: async () => {
        environment.serviceInstances.edges[0].node.latestDeployment.id = 'new-owner';
        return true;
      },
    }),
    /changed while awaiting confirmation/,
  );
  assert.deepEqual(mutations(calls), []);
});

test('a bot removed during confirmation is not removed a second time', async () => {
  const { options, environment, calls } = fixture();
  const result = await switchPreview({
    ...options,
    confirm: async () => {
      const bot = environment.serviceInstances.edges[0].node;
      bot.activeDeployments = [];
      bot.latestDeployment.status = 'REMOVED';
      return true;
    },
  });
  assert.equal(result.changed, true);
  assert.equal(mutations(calls).length, 6);
  assert.ok(mutations(calls).every(({ query }) => query.includes('DeployPreview')));
});

test('sleeping services do not block a switch and sleeping bot removal is confirmed', async () => {
  const { options } = fixture({ initialStatus: 'SLEEPING', firstStopStatus: 'SLEEPING' });
  const result = await switchPreview(options);
  assert.equal(result.changed, true);
  assert.equal(options.now(), 3000);
});

test('a successful old deployment can take a poll to begin removal', async () => {
  const { options } = fixture({ firstStopStatus: 'SUCCESS' });
  assert.equal((await switchPreview(options)).changed, true);
  assert.equal(options.now(), 3000);
});

test('backend failure leaves bot disconnected and stops the deployment sequence', async () => {
  const { options, calls } = fixture({ failService: 'meeting' });
  await assert.rejects(switchPreview(options), /FAILED/);
  assert.equal(
    mutations(calls).some(({ variables }) => variables.serviceId === 'discord-bot'),
    false,
  );
});

test('unconfirmed old-process termination prevents every new deployment', async () => {
  const { options, calls, logs } = fixture({ stuckStop: true });
  await assert.rejects(switchPreview(options), /Timed out.*REMOVING.*deploymentStopped: false/);
  assert.equal(mutations(calls).length, 1);
  assert.ok(logs.some((message) => /REMOVING.*deploymentStopped: false/.test(message)));
});

for (const status of ['CRASHED', 'FAILED', 'SKIPPED', 'NEEDS_APPROVAL', 'REMOVED']) {
  test(`stop fails promptly on ${status} without confirmed termination`, async () => {
    const { options, calls } = fixture({ stopResult: { status, deploymentStopped: false } });
    await assert.rejects(
      switchPreview(options),
      new RegExp(`${status}.*Termination was not confirmed`),
    );
    assert.equal(options.now(), 3000, 'must fail on the terminal status, not the overall timeout');
    assert.equal(mutations(calls).length, 1, 'must not start a new deployment');
  });
}

for (const status of ['CRASHED', 'FAILED', 'SKIPPED', 'NEEDS_APPROVAL', 'REMOVED', 'SLEEPING']) {
  test(`new deployment stops promptly on ${status} instead of waiting for success`, async () => {
    const { options, calls } = fixture({ failService: 'team-tracking', failureStatus: status });
    await assert.rejects(switchPreview(options), new RegExp(`${status}.*switch did not finish`));
    assert.equal(options.now(), 3000);
    assert.equal(mutations(calls).length, 2, 'only the first backend can start');
  });
}

test('a competing deployment stops the switch before the bot can start', async () => {
  const { options, calls } = fixture({ otherDeployment: true });
  await assert.rejects(switchPreview(options), /Another deployment/);
  assert.equal(
    mutations(calls).some(({ variables }) => variables.serviceId === 'discord-bot'),
    false,
  );
});

test('closed, forked, malformed, and production-targeted PRs are refused', () => {
  for (const change of [
    { state: 'CLOSED' },
    { isCrossRepository: true },
    { baseRefName: 'main' },
    { headRefOid: 'main' },
  ]) {
    assert.throws(() => validatePullRequest({ ...PR, ...change }), /open PR/);
  }
});

test('wrong target, missing volume, automatic deploys, and in-flight deploys are refused', () => {
  for (const change of [
    { name: 'staging' },
    { name: 'production' },
    { isEphemeral: true },
    { volumeInstances: connection([]) },
    { deploymentTriggers: connection([{ id: 'trigger' }]) },
  ]) {
    const { environment } = fixture();
    assert.throws(() => validateEnvironment({ ...environment, ...change }));
  }
  const { environment } = fixture();
  environment.serviceInstances.edges[0].node.latestDeployment.status = 'BUILDING';
  assert.throws(() => validateEnvironment(environment), /unfinished deployment/);
  environment.serviceInstances.edges[0].node.latestDeployment.status = 'NEEDS_APPROVAL';
  assert.throws(() => validateEnvironment(environment), /approve or cancel/);
});

test('a preview environment configured as the PR base is refused before changes', async () => {
  const { options, project, calls } = fixture();
  project.baseEnvironmentId = 'dev';
  await assert.rejects(switchPreview(options), /outside the PR base/);
  assert.deepEqual(mutations(calls), []);
});
