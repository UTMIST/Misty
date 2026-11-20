const { text, nodes, managedComment } = require('./automation-common.cjs');

const RAILWAY_PROJECT = 'abf9e8a2-9394-48e2-9d95-a71520c58a5b';
const ENVIRONMENTS = {
  'Misty / staging': 'f03ee072-2333-4352-a0ab-1e27d9e89253',
  'Misty / production': '754fbb40-dd27-436f-96ab-3d12dd5420c8',
};

function railwayUrl(value, environment) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname !== 'railway.com' || url.port || url.username || url.password ||
      !new RegExp(`^/project/${RAILWAY_PROJECT}(?:/service/[a-f0-9-]+)?$`).test(url.pathname) ||
      url.searchParams.get('environmentId') !== environment) return null;
    // Rebuild from known fields; never copy arbitrary query strings into comments.
    const safe = new URL(url.origin + url.pathname);
    safe.searchParams.set('environmentId', environment);
    const id = url.searchParams.get('id');
    if (id && /^[a-f0-9-]+$/.test(id)) safe.searchParams.set('id', id);
    return safe.href;
  } catch { return null; }
}

function serviceEvidence(statuses, deployment, status) {
  const environment = ENVIRONMENTS[deployment.environment];
  const start = Date.parse(deployment.created_at);
  const end = Date.parse(status.created_at);
  if (!Number.isFinite(start) || !Number.isFinite(end)) throw new Error('Missing deployment timestamps');
  const services = new Map();
  for (const item of [...statuses].sort((a, b) => b.id - a.id)) {
    const at = Date.parse(item.created_at);
    const url = railwayUrl(item.target_url, environment);
    if (item.creator?.login !== 'railway-app[bot]' || !url || !item.context.startsWith('Misty - ') ||
      !Number.isFinite(at) || at < start || at > end || services.has(item.context)) continue;
    services.set(item.context, { name: item.context.slice('Misty - '.length), state: item.state, url });
  }
  return [...services.values()].sort((a, b) => a.name.localeCompare(b.name));
}

async function evidence({ github, owner, repo, deploymentId, dryRun = true }) {
  if (!Number.isSafeInteger(deploymentId) || deploymentId <= 0) throw new Error('Invalid deployment ID');
  const { data: deployment } = await github.rest.repos.getDeployment({ owner, repo, deployment_id: deploymentId });
  const environment = ENVIRONMENTS[deployment.environment];
  if (deployment.creator?.login !== 'railway-app[bot]' || !environment || deployment.payload?.environmentId !== environment) {
    return { skipped: 'Not a configured Railway environment', comments: [] };
  }
  if (!/^[a-f0-9]{40}$/i.test(deployment.sha)) throw new Error('Invalid deployed commit');
  // Read current state: queued/out-of-order events must not overwrite success
  // with their stale payload. GitHub does not emit deployment_status for inactive.
  const statuses = await github.paginate(github.rest.repos.listDeploymentStatuses, { owner, repo, deployment_id: deploymentId, per_page: 100 });
  const latest = [...statuses].sort((a, b) => b.id - a.id)[0];
  if (!latest || latest.creator?.login !== 'railway-app[bot]' || latest.state === 'inactive') return { skipped: 'No current Railway deployment status', comments: [] };
  const url = railwayUrl(latest.environment_url, environment) || railwayUrl(latest.log_url, environment);
  if (!url) throw new Error('Deployment status has no trusted Railway evidence URL');
  const commitStatuses = await github.paginate(github.rest.repos.listCommitStatusesForRef, { owner, repo, ref: deployment.sha, per_page: 100 });
  const services = serviceEvidence(commitStatuses, deployment, latest);
  const associated = await github.paginate(github.rest.repos.listPullRequestsAssociatedWithCommit, { owner, repo, commit_sha: deployment.sha, per_page: 100 });
  const targets = new Set();
  for (const candidate of associated) {
    const { data: pr } = await github.rest.pulls.get({ owner, repo, pull_number: candidate.number });
    // Association alone can include an open PR or a later PR containing this
    // commit. Require the exact merged commit before attaching delivery evidence.
    if (!pr.merged_at || pr.merge_commit_sha !== deployment.sha) continue;
    targets.add(pr.number);
    const data = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!) {
      repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
        closingIssuesReferences(first:100) { pageInfo { hasNextPage } nodes {
          number repository { nameWithOwner }
        } }
      } }
    }`, { owner, repo, number: pr.number });
    for (const issue of nodes(data.repository.pullRequest.closingIssuesReferences, 'deployment issue links')) {
      if (issue.repository.nameWithOwner.toLowerCase() === `${owner}/${repo}`.toLowerCase()) targets.add(issue.number);
    }
  }
  const body = `### Deployment evidence\n\n**${text(deployment.environment)}: ${text(latest.state)}** at ${text(latest.created_at)}.\n\nCommit [${deployment.sha.slice(0, 12)}](https://github.com/${owner}/${repo}/commit/${deployment.sha}) · [Railway deployment record](${url}) · GitHub deployment ${deploymentId}.\n\n` +
    (services.length ? `Railway service observations for this commit and environment during this deployment:\n\n| Service | State | Evidence |\n| --- | --- | --- |\n${services.map((s) => `| ${text(s.name)} | ${text(s.state)} | [Railway](${s.url}) |`).join('\n')}\n\n` : 'No service-level observations were available for this deployment window.\n\n') +
    'This records deployment status, not end-to-end acceptance. Only observed services are listed; it does not certify every service was redeployed. Production promotion evidence belongs to the promotion PR; it is not automatically attributed to every earlier feature PR.';
  const comments = [];
  for (const number of targets) comments.push(await managedComment({ github, owner, repo, number,
    key: `deployment:${deploymentId}`, body, dryRun }));
  return { deploymentId, state: latest.state, services, comments };
}

module.exports = { RAILWAY_PROJECT, ENVIRONMENTS, railwayUrl, serviceEvidence, evidence };
