const { test } = require('node:test');
const assert = require('node:assert/strict');
const { blockerNumbers, issuePlan, prStatus, reconcile } = require('./project-board.cjs');

const connection = (nodes) => ({ nodes, pageInfo: { hasNextPage: false, endCursor: null } });
const repository = { nameWithOwner: 'UTMIST/Misty' };
function issue(number, extra = {}) {
  return { __typename: 'Issue', id: `I${number}`, number, state: 'OPEN', body: '', repository,
    labels: connection([]), assignees: { totalCount: 0 }, blockedBy: connection([]), ...extra };
}
function pr(number, extra = {}) {
  return { __typename: 'PullRequest', id: `P${number}`, number, state: 'OPEN', repository,
    isDraft: false, merged: false, reviewDecision: 'REVIEW_REQUIRED', labels: connection([]),
    closingIssuesReferences: connection([]), ...extra };
}
const held = connection([{ name: 'on hold' }]);

test('blocker forms, legacy lines and deduplication; prose is not a hard dependency', () => {
  assert.deepEqual(blockerNumbers('### Blocked by\r\n\r\n#40, #42, #40\r\n## Area\r\n#99'), [40, 42]);
  assert.deepEqual(blockerNumbers('Blocked by: #40, #42\nRelated #99'), [40, 42]);
  assert.deepEqual(blockerNumbers('### Blocked by\n\nNone. Coordinate with #40.\n### What\n#42'), []);
  assert.deepEqual(blockerNumbers('```\nBlocked by: #1\n```\n<!-- Blocked by: #2 -->'), []);
  assert.deepEqual(blockerNumbers('### Blocked by\n\nother/repo#42'), []);
});

test('draft, review, changes requested, approval, merge and abandonment are distinct', () => {
  assert.equal(prStatus(pr(1, { isDraft: true })), 'In progress');
  assert.equal(prStatus(pr(1)), 'In review');
  assert.equal(prStatus(pr(1, { reviewDecision: 'CHANGES_REQUESTED' })), 'In progress');
  assert.equal(prStatus(pr(1, { reviewDecision: 'APPROVED' })), 'In review');
  assert.equal(prStatus(pr(1, { state: 'MERGED', merged: true })), 'Done');
  assert.equal(prStatus(pr(1, { state: 'CLOSED' })), 'Done');
  assert.equal(prStatus(pr(1, { labels: held })), 'Backlog');
});

test('linked PRs move the issue; any unfinished rework wins over another approval', () => {
  assert.equal(issuePlan(issue(1), 'Backlog', [pr(2)], []).status, 'In review');
  assert.equal(issuePlan(issue(1), 'In review', [pr(2, { isDraft: true })], []).status, 'In progress');
  assert.equal(issuePlan(issue(1), 'In review', [pr(2, { reviewDecision: 'APPROVED' }), pr(3, { reviewDecision: 'CHANGES_REQUESTED' })], []).status, 'In progress');
  assert.equal(issuePlan(issue(1), 'In review', [pr(2, { state: 'CLOSED' })], []).status, 'Backlog');
});

test('closing a PR never closes its issue; reopened work never derives Done from an old merge', () => {
  const merged = pr(2, { state: 'MERGED', merged: true });
  assert.equal(issuePlan(issue(1), 'Done', [merged], []).status, 'Backlog');
  assert.equal(issuePlan(issue(1, { state: 'CLOSED' }), 'In review', [], []).status, 'Done');
  assert.equal(issuePlan(issue(1), 'In progress', [merged], []).status, 'In progress');
});

test('resolved blockers do not reset assigned/active/held work to Ready', () => {
  const closed = [{ state: 'CLOSED' }];
  const assigned = issue(1, { assignees: { totalCount: 1 } });
  assert.deepEqual(issuePlan(assigned, 'In progress', [], closed), { status: 'In progress', blocked: false, ready: false });
  assert.equal(issuePlan(issue(1), 'In review', [pr(2)], closed).ready, false);
  assert.deepEqual(issuePlan(issue(1, { labels: held }), 'Backlog', [], closed), { status: 'Backlog', blocked: false, ready: false });
  assert.equal(issuePlan(issue(1), 'Backlog', [], closed).status, 'Ready');
  assert.equal(issuePlan(issue(1), 'Backlog', [], []).status, 'Backlog');
});

test('open/unknown blockers remove availability without a nonexistent Blocked column', () => {
  assert.deepEqual(issuePlan(issue(1), 'Ready', [], [{ state: 'UNKNOWN' }]), { status: 'Backlog', blocked: true, ready: false });
  assert.deepEqual(issuePlan(issue(1), 'In progress', [], [{ state: 'OPEN' }]), { status: 'In progress', blocked: true, ready: false });
  assert.equal(issuePlan(issue(1, { state: 'CLOSED' }), 'Done', [], [{ state: 'OPEN' }]).blocked, false);
  assert.equal(issuePlan(issue(1), 'In review', [pr(2, { labels: held })], []).status, 'Backlog');
});

function fixture({ issues = [], prs = [], items = [], missingStatus, unavailableBlocker, currentIssueState = 'closed', pageSize = 100 } = {}) {
  const writes = [];
  const reads = [];
  const statuses = ['Backlog', 'Ready', 'In progress', 'In review', 'Done'].filter((s) => s !== missingStatus);
  const paginate = (nodes, cursor) => {
    const start = Number(cursor || 0);
    const end = start + pageSize;
    return { nodes: nodes.slice(start, end), pageInfo: { hasNextPage: end < nodes.length, endCursor: String(end) } };
  };
  const graphql = async (query, vars) => {
    if (query.startsWith('mutation')) {
      writes.push({ query, vars });
      if (query.includes('addProjectV2ItemById')) {
        const content = [...issues, ...prs].find((x) => x.id === vars.content);
        const item = { id: `item-${content.id}`, isArchived: false, content, fieldValueByName: { name: 'Backlog' } };
        items.push(item);
        return { addProjectV2ItemById: { item } };
      }
      if (query.includes('unarchiveProjectV2Item')) items.find((i) => i.id === vars.item).isArchived = false;
      if (query.includes('updateProjectV2ItemFieldValue')) items.find((i) => i.id === vars.item).fieldValueByName = { name: vars.option };
      return {};
    }
    reads.push(query);
    if (query.includes('field(name:')) return { node: { id: 'project', field: { id: 'status', options: statuses.map((name) => ({ name, id: name })) } } };
    if (query.includes('items(first:')) return { node: { items: paginate(items, vars.cursor) } };
    if (query.includes('issues(first:')) return { repository: { issues: paginate(issues, vars.cursor) } };
    if (query.includes('pullRequests(first:')) return { repository: { pullRequests: paginate(prs, vars.cursor) } };
    throw new Error(`Unexpected query: ${query}`);
  };
  const github = { rest: { issues: {
    get: async () => {
      if (unavailableBlocker) throw Object.assign(new Error('unavailable'), { status: unavailableBlocker });
      return { data: { state: currentIssueState } };
    },
    addLabels: async (args) => {
      writes.push({ addLabels: args });
      const target = issues.find((i) => i.number === args.issue_number);
      target.labels.nodes.push(...args.labels.map((name) => ({ name })));
    },
    removeLabel: async (args) => {
      writes.push({ removeLabel: args });
      const target = issues.find((i) => i.number === args.issue_number) || items.find((i) => i.content.number === args.issue_number).content;
      target.labels.nodes = target.labels.nodes.filter((l) => l.name !== args.name);
    },
  } } };
  return { graphql, github, owner: 'UTMIST', repo: 'Misty', log: () => {}, writes, reads, items };
}
function card(content, status, isArchived = false) {
  return { id: `item-${content.id}`, content, isArchived, fieldValueByName: { name: status }, priority: 'P1' };
}

test('full reconciliation adds missing cards, links review status, paginates and is idempotent', async () => {
  const work = issue(1);
  const review = pr(2, { closingIssuesReferences: connection([{ id: work.id, number: 1, repository }]) });
  const f = fixture({ issues: [work, issue(3)], prs: [review], pageSize: 1 });
  const first = await reconcile({ ...f, dryRun: false });
  assert.equal(first.changes.filter((c) => c.add && c.to).length, 3);
  assert.equal(f.items.find((i) => i.content.id === work.id).fieldValueByName.name, 'In review');
  const second = await reconcile({ ...f, dryRun: false });
  assert.deepEqual(second.changes, []);
  assert.ok(f.reads.filter((q) => q.includes('issues(first:')).length >= 4);
  assert.ok(f.writes.every((w) => !w.query || (!w.query.includes('closeIssue') && !w.query.includes('updateIssue'))));
});

test('dry run validates live-shaped data but writes nothing', async () => {
  const f = fixture({ issues: [issue(1)] });
  const result = await reconcile(f);
  assert.equal(result.changes.length, 1);
  assert.deepEqual(f.writes, []);
});

test('closed cards, stale labels, native blockers and reopened archives reconcile without touching priority', async () => {
  const closed = issue(1, { state: 'CLOSED', labels: connection([{ name: 'ready' }]) });
  const reopened = issue(2, { blockedBy: connection([{ state: 'OPEN', repository: { nameWithOwner: 'other/repo' } }]) });
  const archived = card(reopened, 'Done', true);
  const f = fixture({ issues: [reopened], items: [card(closed, 'Ready'), archived] });
  await reconcile({ ...f, dryRun: false });
  assert.equal(f.items[0].fieldValueByName.name, 'Done');
  assert.deepEqual(closed.labels.nodes, []);
  assert.equal(archived.isArchived, false);
  assert.equal(archived.fieldValueByName.name, 'Backlog');
  assert.equal(archived.priority, 'P1');
  assert.ok(reopened.labels.nodes.some((l) => l.name === 'blocked'));
});

test('missing field option and truncated relationships fail before any mutations', async () => {
  const missing = fixture({ issues: [issue(1)], missingStatus: 'In review' });
  await assert.rejects(reconcile({ ...missing, dryRun: false }), /Missing project Status/);
  assert.deepEqual(missing.writes, []);
  const truncated = fixture({ issues: [issue(1)], prs: [pr(2, { closingIssuesReferences: { nodes: [], pageInfo: { hasNextPage: true } } })] });
  await assert.rejects(reconcile({ ...truncated, dryRun: false }), /Incomplete closing links/);
  assert.deepEqual(truncated.writes, []);
});

test('inaccessible blockers fail closed; authentication/server errors fail the run visibly', async () => {
  const blocked = issue(1, { body: 'Blocked by: #99' });
  const missing = fixture({ issues: [blocked], unavailableBlocker: 404 });
  const result = await reconcile(missing);
  assert.ok(result.changes.some((c) => c.label === 'blocked' && c.add));
  const outage = fixture({ issues: [blocked], unavailableBlocker: 503 });
  await assert.rejects(reconcile({ ...outage, dryRun: false }), /unavailable/);
  assert.deepEqual(outage.writes, []);
});

test('unrelated repository cards and archived closed history are untouched', async () => {
  const f = fixture({ items: [card(issue(1, { repository: { nameWithOwner: 'other/repo' } }), 'Ready'), card(issue(2, { state: 'CLOSED' }), 'Done', true), card(pr(3, { state: 'MERGED', merged: true }), 'Done', true)] });
  await reconcile({ ...f, dryRun: false });
  assert.deepEqual(f.writes, []);
});

test('a concurrent reopen cannot be undone through native Done-to-close automation', async () => {
  const f = fixture({ items: [card(issue(1, { state: 'CLOSED' }), 'In progress')], currentIssueState: 'open' });
  const result = await reconcile({ ...f, dryRun: false });
  assert.equal(result.changes[0].skipped, 'Issue reopened during reconciliation');
  assert.deepEqual(f.writes, []);
});

test('consolidated reports are excluded while health incidents remain on the working board', async () => {
  const f = fixture({ issues: [issue(1, { labels: connection([{ name: 'automation/report' }]) }), issue(2, { labels: connection([{ name: 'automation/health' }]) })] });
  const result = await reconcile(f);
  assert.deepEqual(result.changes.map((c) => c.number), [2]);
});
