import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  previewServices,
  switchPreview,
  validateEnvironment,
  validatePullRequest,
} from '../scripts/lib/preview.js';
import { railwayApi } from '../scripts/lib/previewCli.js';

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
  removedLagReads = 0,
  buildDelayMs = 0,
  historyPageSize = 100,
  omitOldFromHistory = false,
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
  const source = {
    truncated: false,
    tree: [
      { path: 'discord-bot/src/runtimeMode.js', type: 'blob' },
      ...names
        .filter((name) => name !== 'discord-bot')
        .concat('connectors')
        .map((name) => ({ path: `services/${name}`, type: 'tree' })),
    ],
  };
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
  const botHistory = [{ id: 'discord-bot-old', status: initialStatus, deploymentStopped: false }];
  const runningBots = new Set(['discord-bot-old']);
  const stopRequested = new Set();
  const stopReads = new Map();
  const sleeps = [];
  let clock = 0;
  let buildStartedAt;
  const api = async (query, variables) => {
    calls.push({ query, variables });
    const instances = environment.serviceInstances.edges.map(({ node }) => node);
    if (query.includes('query PreviewProjectToken')) {
      return {
        projectToken: {
          projectId: project.id,
          environmentId: environment.id,
          project: { baseEnvironmentId: project.baseEnvironmentId },
        },
      };
    }
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
    if (query.includes('query PreviewBotDeployments')) {
      assert.equal(variables.environmentId, 'dev');
      assert.equal(variables.serviceId, 'discord-bot');
      assert.match(query, /includeDeleted: true/);
      const bot = instances.find((s) => s.serviceId === 'discord-bot');
      const head = botHistory.find((deployment) => deployment.id === bot.latestDeployment?.id);
      if (head) head.status = bot.latestDeployment.status;
      const visible = botHistory.filter(
        (deployment) => !omitOldFromHistory || deployment.id !== 'discord-bot-old',
      );
      const offset = Number(variables.after || 0);
      const next = offset + historyPageSize;
      return {
        deployments: {
          ...connection(structuredClone(visible.slice(offset, next))),
          pageInfo: { hasNextPage: next < visible.length, endCursor: String(next) },
        },
      };
    }
    if (query.includes('mutation StopPreview')) {
      assert.ok(botHistory.some((deployment) => deployment.id === variables.id));
      stopRequested.add(variables.id);
      return { deploymentRemove: true };
    }
    if (query.includes('query PreviewDeployment')) {
      const old = botHistory.find((deployment) => deployment.id === variables.id);
      if (old) {
        const bot = instances.find((s) => s.serviceId === 'discord-bot');
        if (bot.latestDeployment?.id === old.id && bot.latestDeployment.status === 'REMOVED')
          old.status = 'REMOVED';
        const requested = stopRequested.has(old.id);
        if (!requested && !['REMOVED', 'REMOVING'].includes(old.status))
          return { deployment: structuredClone(old) };
        const reads = (stopReads.get(old.id) || 0) + 1;
        stopReads.set(old.id, reads);
        if (requested && (stuckStop || reads === 1)) {
          return {
            deployment: { id: variables.id, status: firstStopStatus, deploymentStopped: false },
          };
        }
        if (stopResult) return { deployment: { id: variables.id, ...stopResult } };
        if (reads <= removedLagReads + Number(requested)) {
          return { deployment: { id: variables.id, status: 'REMOVED', deploymentStopped: false } };
        }
        bot.activeDeployments = bot.activeDeployments.filter(
          (deployment) => deployment.id !== old.id,
        );
        if (bot.latestDeployment?.id === old.id) bot.latestDeployment.status = 'REMOVED';
        old.status = 'REMOVED';
        old.deploymentStopped = true;
        runningBots.delete(old.id);
        return { deployment: { id: variables.id, status: 'REMOVED', deploymentStopped: true } };
      }
      const service = instances.find((s) => s.latestDeployment?.id === variables.id);
      assert.ok(service);
      if (service.serviceId === 'meeting' && clock - buildStartedAt < buildDelayMs) {
        return { deployment: { ...service.latestDeployment, deploymentStopped: false } };
      }
      service.latestDeployment.status =
        service.serviceId === failService ? failureStatus : 'SUCCESS';
      service.activeDeployments = [service.latestDeployment];
      return { deployment: { ...service.latestDeployment, deploymentStopped: false } };
    }
    if (query.includes('mutation DeployPreview')) {
      assert.equal(runningBots.size, 0, 'must wait until the previous bot has actually stopped');
      assert.equal(variables.environmentId, 'dev');
      assert.equal(variables.commitSha, PR.headRefOid);
      const service = instances.find((s) => s.serviceId === variables.serviceId);
      service.latestDeployment = { id: `${service.serviceId}-new`, status: 'BUILDING' };
      buildStartedAt = clock;
      return { serviceInstanceDeployV2: service.latestDeployment.id };
    }
    throw new Error(`Unexpected API query: ${query}`);
  };
  return {
    environment,
    project,
    source,
    calls,
    logs,
    botHistory,
    runningBots,
    sleeps,
    options: {
      projectId: 'project',
      pr: PR,
      readSource: async (sha) => {
        assert.equal(sha, PR.headRefOid);
        return structuredClone(source);
      },
      api,
      confirm: async () => true,
      log: (message) => logs.push(message),
      now: () => clock,
      sleep: async (ms) => {
        sleeps.push(ms);
        clock += ms;
      },
      timeoutMs: 10_000,
      stopTimeoutMs: 10_000,
    },
  };
}

const mutations = (calls) => calls.filter(({ query }) => query.startsWith('mutation'));

test('switch waits for disconnect, pins all services to the PR commit, and deploys bot last', async () => {
  const { options, calls, logs } = fixture();
  const result = await switchPreview(options);
  assert.equal(result.changed, true);
  assert.ok(logs.some((line) => /connectors.*will not be tested/.test(line)));
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

test('a dev-scoped project token switches without enumerating project environments', async () => {
  const { options, calls } = fixture();
  const result = await switchPreview({
    ...options,
    projectToken: true,
    api: (query, variables) => {
      assert.doesNotMatch(query, /query PreviewProject\(/);
      assert.doesNotMatch(query, /environments\s*\{/);
      if (query.includes('query PreviewEnvironment')) assert.equal(variables.id, 'dev');
      return options.api(query, variables);
    },
  });
  assert.equal(result.changed, true);
  assert.match(calls[0].query, /PreviewProjectToken/);
});

test('project tokens for another project, a non-dev environment, or the PR base fail preflight', async () => {
  for (const mismatch of ['project', 'environment', 'base', 'ephemeral']) {
    const { options, project, environment, calls } = fixture();
    if (mismatch === 'project') project.id = 'other-project';
    if (mismatch === 'environment') environment.name = 'staging';
    if (mismatch === 'base') project.baseEnvironmentId = 'dev';
    if (mismatch === 'ephemeral') environment.isEphemeral = true;
    await assert.rejects(
      switchPreview({ ...options, projectToken: true }),
      /RAILWAY_TOKEN|persistent dev environment/,
    );
    assert.deepEqual(mutations(calls), []);
  }
});

for (const [field, operation] of [
  ['project', 'PreviewProject('],
  ['projectToken', 'PreviewProjectToken'],
  ['environment', 'PreviewEnvironment'],
  ['deployments', 'PreviewBotDeployments'],
  ['deployment', 'PreviewDeployment'],
]) {
  test(`a null ${field} stops the selector with an API diagnostic before any new deployment`, async () => {
    const { options, calls } = fixture();
    await assert.rejects(
      switchPreview({
        ...options,
        projectToken: field === 'projectToken',
        api: (query, variables) =>
          railwayApi(query, variables, async () => ({
            stdout: JSON.stringify({
              data: query.includes(operation)
                ? { [field]: null }
                : await options.api(query, variables),
            }),
          })),
      }),
      new RegExp(`Railway API returned no ${field}.*deleted or access changed`),
    );
    assert.ok(mutations(calls).every(({ query }) => query.includes('StopPreview')));
  });
}

test('branches without preview support fail before any Railway read or mutation', async () => {
  const { source, options, calls } = fixture();
  source.tree = source.tree.filter((entry) => entry.path !== 'discord-bot/src/runtimeMode.js');
  await assert.rejects(switchPreview(options), /lacks preview runtime support.*Update its branch/);
  assert.deepEqual(calls, []);
});

test('truncated GitHub trees fail before any Railway read or mutation', async () => {
  const { source, options, calls } = fixture();
  source.truncated = true;
  await assert.rejects(switchPreview(options), /incomplete source tree/);
  assert.deepEqual(calls, []);
});

test('new service directories must be provisioned before the bot is stopped', async () => {
  const { source, options, calls } = fixture();
  source.tree.push({ path: 'services/scheduler', type: 'tree' });
  await assert.rejects(switchPreview(options), /dev is missing scheduler/);
  assert.deepEqual(mutations(calls), []);
});

test('a newly provisioned backend from the PR inventory deploys before the bot', async () => {
  const { source, environment, options, calls } = fixture();
  source.tree.push({ path: 'services/scheduler', type: 'tree' });
  environment.serviceInstances.edges.push({
    node: {
      serviceId: 'scheduler',
      serviceName: 'scheduler',
      source: { repo: 'UTMIST/Misty' },
      latestDeployment: null,
      activeDeployments: [],
    },
  });
  assert.equal((await switchPreview(options)).changed, true);
  assert.deepEqual(
    mutations(calls)
      .slice(-2)
      .map(({ variables }) => variables.serviceId),
    ['scheduler', 'discord-bot'],
  );
});

test('services present only in Railway fail preflight with a named mismatch', async () => {
  const { source, options, calls } = fixture();
  source.tree = source.tree.filter((entry) => entry.path !== 'services/meeting');
  await assert.rejects(switchPreview(options), /meeting.*absent from this PR/);
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

test('manual removal still waits for lagging instance shutdown before restarting backends', async () => {
  const { options, environment, calls } = fixture({ removedLagReads: 2 });
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
  assert.equal(options.now(), 6000);
  assert.ok(mutations(calls).every(({ query }) => query.includes('DeployPreview')));
});

test('a newer failed build does not hide the older gateway deployment', async () => {
  const { options, environment, botHistory, calls } = fixture({ historyPageSize: 1 });
  const failed = { id: 'failed-build', status: 'FAILED', deploymentStopped: true };
  botHistory.unshift(failed);
  environment.serviceInstances.edges[0].node.latestDeployment = { ...failed };
  assert.equal((await switchPreview(options)).changed, true);
  assert.equal(mutations(calls)[0].variables.id, 'discord-bot-old');
  assert.equal(
    calls.filter(({ query }) => query.includes('query PreviewBotDeployments')).length,
    2,
  );
  assert.ok(botHistory.every((deployment) => deployment.deploymentStopped));
});

test('history finds an older draining gateway hidden by a failed head and an empty active list', async () => {
  const { options, environment, botHistory, calls } = fixture({
    historyPageSize: 1,
    removedLagReads: 2,
  });
  const failed = { id: 'failed-build', status: 'FAILED', deploymentStopped: true };
  botHistory[0].status = 'REMOVED';
  botHistory[0].instances = [{ status: 'REMOVING' }];
  botHistory.unshift(failed);
  const bot = environment.serviceInstances.edges[0].node;
  bot.latestDeployment = { ...failed };
  bot.activeDeployments = [];
  assert.equal((await switchPreview(options)).changed, true);
  assert.equal(options.now(), 6000);
  assert.ok(mutations(calls).every(({ query }) => query.includes('DeployPreview')));
});

test('stale failed, skipped, and removed history with no live instances never enters the stop set', async () => {
  const { options, botHistory, calls } = fixture();
  const stale = ['FAILED', 'SKIPPED', 'REMOVED'].flatMap((status) =>
    [[], [{ status: 'STOPPED' }], [{ status: 'REMOVED' }], [{ status: 'CRASHED' }]].map(
      (instances, index) => ({
        id: `stale-${status}-${index}`,
        status,
        deploymentStopped: false,
        instances,
      }),
    ),
  );
  botHistory.push(...stale);
  assert.equal((await switchPreview(options)).changed, true);
  assert.ok(calls.every(({ variables }) => !variables.id?.startsWith('stale-')));
  assert.ok(stale.every((deployment) => !deployment.deploymentStopped));
});

test('a failed latest build with a stale stop flag is ignored while the older gateway is stopped', async () => {
  const { options, botHistory, environment, calls } = fixture();
  const failed = { id: 'failed-build', status: 'FAILED', deploymentStopped: false, instances: [] };
  botHistory.unshift(failed);
  environment.serviceInstances.edges[0].node.latestDeployment = { ...failed };
  assert.equal((await switchPreview(options)).changed, true);
  assert.ok(calls.every(({ variables }) => variables.id !== failed.id));
});

test('IDs observed before confirmation remain tracked if removal hides them from history', async () => {
  const { options, environment, botHistory, calls } = fixture({
    omitOldFromHistory: true,
    removedLagReads: 2,
  });
  const failed = { id: 'failed-build', status: 'FAILED', deploymentStopped: true };
  botHistory.unshift(failed);
  const bot = environment.serviceInstances.edges[0].node;
  bot.latestDeployment = { ...failed };
  assert.equal(
    (
      await switchPreview({
        ...options,
        confirm: async () => {
          bot.activeDeployments = [];
          botHistory.find((deployment) => deployment.id === 'discord-bot-old').status = 'REMOVED';
          return true;
        },
      })
    ).changed,
    true,
  );
  assert.ok(options.now() > 0);
  assert.ok(mutations(calls).every(({ query }) => query.includes('DeployPreview')));
});

test('all prior gateway deployments share the stop deadline', async () => {
  const { options, botHistory, runningBots, calls } = fixture({ removedLagReads: 2 });
  botHistory.push({
    id: 'older-gateway',
    status: 'REMOVED',
    deploymentStopped: false,
    instances: [{ status: 'REMOVING' }],
  });
  runningBots.add('older-gateway');
  await assert.rejects(switchPreview(options), /Timed out/);
  assert.equal(options.now(), options.stopTimeoutMs);
  assert.equal(mutations(calls).length, 1);
});

test('a removal that exhausts the stop budget fails clearly without polling or deploying', async () => {
  const { options, calls } = fixture();
  await assert.rejects(
    switchPreview({
      ...options,
      api: async (query, variables) => {
        const result = await options.api(query, variables);
        if (query.includes('mutation StopPreview')) await options.sleep(options.stopTimeoutMs + 1);
        return result;
      },
    }),
    { message: 'Timed out confirming previous bot shutdown. No new deployments were started.' },
  );
  assert.equal(mutations(calls).length, 1);
  assert.ok(calls.every(({ query }) => !query.includes('query PreviewDeployment')));
});

test('an unfinished bot deployment in history fails before any removal', async () => {
  const { options, botHistory, calls } = fixture();
  botHistory.unshift({ id: 'older-build', status: 'BUILDING', deploymentStopped: true });
  await assert.rejects(switchPreview(options), /older-build.*BUILDING.*resolve it/);
  assert.deepEqual(mutations(calls), []);
});

test('a crash-looping bot can be removed even when its first stop poll still reports CRASHED', async () => {
  const { options, environment } = fixture({ firstStopStatus: 'CRASHED' });
  environment.serviceInstances.edges[0].node.latestDeployment.status = 'CRASHED';
  assert.equal((await switchPreview(options)).changed, true);
  assert.equal(options.now(), 3000);
});

test('a sleeping backend is accepted and the gateway bot is deployed last', async () => {
  const { options, calls, logs } = fixture({
    failService: 'team-tracking',
    failureStatus: 'SLEEPING',
  });
  assert.equal((await switchPreview(options)).changed, true);
  assert.match(
    logs.find((line) => line.startsWith('team-tracking:')),
    /sleeping/,
  );
  assert.equal(mutations(calls).at(-1).variables.serviceId, 'discord-bot');
});

test('a sleeping gateway bot cannot complete the switch', async () => {
  const { options } = fixture({ failService: 'discord-bot', failureStatus: 'SLEEPING' });
  await assert.rejects(switchPreview(options), /SLEEPING.*switch did not finish/);
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

test('REMOVED waits through lagging shutdown confirmation before deploying', async () => {
  const { options, calls } = fixture({ removedLagReads: 2 });
  assert.equal((await switchPreview(options)).changed, true);
  assert.equal(options.now(), 9000);
  assert.equal(mutations(calls).length, 7);
});

test('REMOVED without shutdown confirmation waits until the stop deadline', async () => {
  const { options, calls } = fixture({
    stopResult: { status: 'REMOVED', deploymentStopped: false },
  });
  await assert.rejects(switchPreview(options), /Timed out.*REMOVED.*deploymentStopped: false/);
  assert.ok(options.now() >= options.stopTimeoutMs);
  assert.equal(mutations(calls).length, 1);
});

test('the default timeout allows a cold meeting build longer than fifteen minutes', async () => {
  const { options, calls, sleeps } = fixture({ buildDelayMs: 20 * 60_000 });
  delete options.timeoutMs;
  assert.equal((await switchPreview(options)).changed, true);
  assert.ok(options.now() > 20 * 60_000);
  assert.ok(
    calls.filter(
      ({ query, variables }) =>
        query.includes('query PreviewDeployment') && variables.id === 'meeting-new',
    ).length < 60,
  );
  assert.ok(Math.max(...sleeps) <= 30_000);
});

test('a custom deployment timeout still bounds unfinished builds', async () => {
  const { options, calls } = fixture({ buildDelayMs: 20 * 60_000 });
  await assert.rejects(switchPreview(options), /Timed out.*meeting-new.*BUILDING/);
  assert.equal(
    mutations(calls).some(({ variables }) => variables.serviceId === 'discord-bot'),
    false,
  );
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

for (const status of ['CRASHED', 'FAILED', 'SKIPPED', 'NEEDS_APPROVAL']) {
  test(`stop keeps polling stale ${status} until its deadline without starting deployments`, async () => {
    const { options, calls } = fixture({ stopResult: { status, deploymentStopped: false } });
    await assert.rejects(switchPreview(options), new RegExp(`Timed out.*${status}`));
    assert.equal(options.now(), options.stopTimeoutMs);
    assert.equal(mutations(calls).length, 1, 'must not start a new deployment');
  });
}

for (const status of ['CRASHED', 'FAILED', 'SKIPPED', 'NEEDS_APPROVAL', 'REMOVED']) {
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

for (const initialDeployment of ['existing', 'none']) {
  test(`a competing deployment of a pending backend (${initialDeployment}) is never overwritten`, async () => {
    const { options, calls, environment } = fixture();
    const meeting = environment.serviceInstances.edges.find(
      ({ node }) => node.serviceId === 'meeting',
    ).node;
    if (initialDeployment === 'none') meeting.latestDeployment = null;
    await assert.rejects(
      switchPreview({
        ...options,
        api: async (query, variables) => {
          const result = await options.api(query, variables);
          if (query.includes('query PreviewDeployment') && variables.id === 'team-tracking-new') {
            meeting.latestDeployment = { id: 'another-operators-meeting', status: 'SUCCESS' };
          }
          return result;
        },
      }),
      /Another deployment changed dev/,
    );
    assert.deepEqual(
      mutations(calls)
        .filter(({ query }) => query.includes('DeployPreview'))
        .map(({ variables }) => variables.serviceId),
      ['team-tracking'],
    );
  });
}

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
    const { environment, source } = fixture();
    assert.throws(() =>
      validateEnvironment({ ...environment, ...change }, previewServices(source)),
    );
  }
  const { environment, source } = fixture();
  environment.serviceInstances.edges[0].node.latestDeployment.status = 'BUILDING';
  assert.throws(
    () => validateEnvironment(environment, previewServices(source)),
    /unfinished deployment/,
  );
  environment.serviceInstances.edges[0].node.latestDeployment.status = 'NEEDS_APPROVAL';
  assert.throws(
    () => validateEnvironment(environment, previewServices(source)),
    /approve or cancel/,
  );
});

test('a preview environment configured as the PR base is refused before changes', async () => {
  const { options, project, calls } = fixture();
  project.baseEnvironmentId = 'dev';
  await assert.rejects(switchPreview(options), /outside the PR base/);
  assert.deepEqual(mutations(calls), []);
});
