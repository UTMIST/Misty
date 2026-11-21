# GitHub Repository Q&A: Implementation Plan

This is the agreed direction for [#333](https://github.com/UTMIST/Misty/issues/333):
let Misty answer questions about source code and documentation in UTMIST GitHub
repositories. Use this plan to create future implementation issues. Nothing
below is implemented by this documentation change. Detailed API contracts,
budgets, and rollout checks belong in the implementation issues.

Today, connectors fetches a supplied README or text-file URL through REST. It
does not search a repository or return GitHub access grants. See the existing
[fetch contract](../services/connectors/docs/API.md#post-fetch).

## The phases at a glance

| Phase | Repositories | User GitHub authentication | Where answers appear |
|---|---|---|---|
| 1. Public repository Q&A | Enrolled public UTMIST repositories only | Not required | Public answers in supported Discord channels; private answers in DMs |
| 2. Individual access | Also enrolled private UTMIST repositories the user can read | Required for private retrieval | DMs, or a private interaction in the requesting Discord channel |
| 3. Team access | Also private UTMIST repositories shared with mapped teams | Not required for channel-authorized answers; still required for personal private answers | Publicly within authorized channels; private answers follow phase 2 |

Each phase preserves earlier functionality. Personal repositories and repositories
owned by other organizations remain out of scope in every phase, even if a user
or backend credential can read them. “Any private repository the user can access”
means any **enrolled UTMIST repository** accessible to both that user and the App.

“Anywhere on Discord” means supported servers/channels where Misty is installed,
allowed to respond, and can receive the message, plus its DMs. Existing active
Misty-account and bot activation rules still apply. Phase 1 must not require a
channel team assignment for public GitHub retrieval. It must not broaden access
to private documents through the existing document-search tool.

## Phase 1: public UTMIST repository Q&A

### What users can do

A Misty user can mention Misty in a supported Discord channel, or message it in a
DM, and ask questions without knowing file paths. For example: “Where is
permissions checking implemented in Misty?” The answer should cite the relevant
files and commit, explain incomplete evidence, and distinguish code behavior
from what the documentation claims.

Only public UTMIST repositories are eligible. Maintain an explicit enrollment
list, with any existing branch available for questions. Expanding that list is a
maintainer operation; the model cannot enroll repositories. A private repository
must be denied even when the App's token could fetch it.

### GitHub App and MCP setup

Register the Misty GitHub App under UTMIST and restrict installation availability
to **Only on this account**. Install it on selected UTMIST repositories with
Contents read access and required metadata access. No repository writes or team
management permissions are needed for phase 1. Public App visibility versus
private App visibility describes installation availability, not repository
visibility. See [GitHub App installation availability](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private).

Use the official GitHub MCP server with backend authentication, read-only mode,
and a reviewed allowlist of repository search/navigation and file-read tools.
MCP requires authentication even for public reads; members do not need their own
GitHub login. Validate organization and repository IDs in backend code on every
call, including search. Restricting App installations alone does not prevent
reading public repositories outside UTMIST.

### Architecture decision: use existing Misty services

**This work must not create a new Misty service.** Implement all three phases by
extending the existing services below. MCP is a provider/tool interface owned
behind the connectors boundary.

| Existing component | Proposed responsibility |
|---|---|
| Discord bot | Accept questions, derive trusted person/channel context, select response visibility, and deliver answers |
| connectors | Own GitHub credentials, MCP transport, bounded source calls, and normalized provider errors behind authenticated HTTP |
| documentation-system | Own repository enrollment and retrieval authorization/orchestration; any future source index or content cache |
| llm | Continue providing model calls through the existing boundary |
| team-tracking | Continue owning Misty identities, teams, memberships, and channel assignments |

An MCP server may run as a child process or separately hosted provider process;
that operational choice is different from adding a new Misty domain service.
Choose hosted versus self-hosted MCP after checking tool coverage, token support,
process isolation, and deployment cost. The local server supports GitHub App
installation authentication; HTTP clients supply their own token. See
[App authentication](https://github.com/github/github-mcp-server/blob/main/docs/github-app-auth.md)
and [host integration](https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md).

The new GitHub retrieval path needs its own explicit public-source policy. Do not
make the existing catalog search unfiltered or use its direct-read `SEE_ALL`
fallback to support public GitHub questions.

### Suggested implementation issues and acceptance

1. **App and MCP provider setup:** choose the transport and pinned server
   version, validate installation-token support for the exact tools, and expose
   a small authenticated HTTP contract through connectors. Keep the existing
   single-file fetch contract working.
2. **Repository enrollment and retrieval:** store stable IDs and branch policy;
   implement public-visibility checks, bounded search/navigation, and commit-pinned
   file reads. Specify supported text/code formats and numerical budgets.
3. **Discord Q&A:** wire the retrieval tool into mentions and DMs with citations;
   allow public GitHub questions in supported channels without channel teams.
   Preserve existing document access checks.
4. **Evaluation and rollout:** compare with the current single-file baseline
   using #333's cross-file questions. Test public-to-private changes, non-UTMIST
   and unenrolled repositories, truncated results, prompt injection, and costs.

Phase 1 is ready when an eligible user without a GitHub link can get a useful,
attributed public answer, while private repositories never enter model context,
caches used for public answers, or public responses. Check visibility before
retrieval and publication; stop serving cached evidence if visibility changes.

## Phase 2: individual GitHub authentication and private answers

### What users can do

A member connects their GitHub account to Misty using GitHub's real browser
OAuth authorization flow. They can then ask about enrolled private UTMIST
repositories they can currently read, including repositories they collaborate on
rather than own. Public Q&A continues without individual GitHub authentication.

In DMs, private answers stay in that one-to-one conversation. In channels,
phase-2 GitHub answers are private to the requester; personal access never
justifies a normal public channel reply. An ordinary mention cannot directly
receive an ephemeral answer: reuse Misty's requester-only **View privately**
button, then perform private retrieval and answer through the ephemeral
interaction. A slash command can acknowledge privately immediately. Keep public
acknowledgements generic, without private repository names, snippets, or links.
If private delivery fails or expires, withhold the answer rather than posting it
publicly. The user's original channel question itself is still visible there;
use DMs for sensitive questions.

This follows the bot's existing
[private response flow](../discord-bot/README.md#commands-through-mentions-and-dms)
and Discord's [interaction response model](https://docs.discord.com/developers/interactions/receiving-and-responding).
Phase 1 public-only answers can remain public; requests invoking individual
private access in phase 2 stay private, including mixed public/private answers.

### Authentication and App installation

**Users authorize the App; UTMIST installs it.** Do not ask each member to install
Misty on their personal account. The organization installation selects the repos;
individual authorization gives Misty a user access token. GitHub limits that token
to resources both the user and the App can access. Authenticating a member does
not grant access to their personal repos or install the App anywhere else. See
[GitHub's user authorization model](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user).

Use GitHub's standard browser OAuth flow for individual authentication and
authorization. GitHub handles sign-in under the account's existing authentication
policies; Misty receives the authorization callback and exchanges its code for a
user access token. Misty must not collect GitHub passwords or implement an
additional authentication challenge.

Bind OAuth initiation and callback to the authenticated Misty person and Discord
user using unpredictable, single-use, expiring state and a fixed validated
callback URL. Follow GitHub's current authorization flow and use PKCE where
supported by that flow. Verify the account returned by GitHub, and store its
stable numeric ID with its current login. An entered username or email match is
not ownership verification. Re-linking needs an explicit account-change flow.
See [GitHub user token generation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app).

Team-tracking owns the verified person/account association. Credentials remain
behind the connector/provider boundary, outside directory records and model
context. Choose and document a durable encrypted credential store and its owner;
connectors currently has no database. Enable expiring user tokens and implement
refresh, unlinking, and authorization-revocation handling. Select the requester's
credential server-side; never fall back to the App's broader installation token
or another user's token when individual authorization fails.

### Suggested implementation issues and acceptance

1. **Verified account linking:** add the browser/callback flow, account ID
   representation, and reconnect/unlink UX.
2. **Credential lifecycle:** choose storage and encryption, implement token
   refresh/revocation and secret-safe auditing, and verify MCP user-token support
   and per-user session isolation.
3. **Private retrieval and delivery:** enforce repository enrollment plus current
   user/App access before retrieval and publication; add DM and ephemeral channel
   responses without shared-history leakage.
4. **Offline access tests:** cover callback replay/account swapping, missing or
   revoked tokens, refresh failure, blocked private delivery, and two users with
   different access to the same repository.

Phase 2 is ready when private answers are delivered only to the authenticated
requester, access revocation stops cached/history reuse, and phase-1 public Q&A
still works without a GitHub link. A request initiated in a channel uses person
context for its private retrieval, not that channel's shared grants. Keep public
channel history separate from the private continuation and its evidence.

## Phase 3: team-based public answers within authorized channels

### Team mapping and source of truth

Create a one-to-one relationship between each **participating** Misty team and
one GitHub team inside UTMIST. Use stable IDs and enforce uniqueness in both
directions; names/slugs are labels, not identity. Existing unrelated GitHub or
Misty teams need not be enrolled automatically.

Misty remains the source of truth for its teams, active memberships, and Discord
channel assignments. GitHub is authoritative for which repositories a GitHub team
can read. Map those repository grants onto the corresponding Misty team IDs.
The target membership policy is Misty-to-GitHub for managed teams, not two
competing membership sources. Before enabling automated membership writes,
review existing GitHub members, outside collaborators, and nested teams; decide
how to adopt or preserve them and preview removals. Roll out read-only mapping
first. Use a separate, narrowly permissioned team-management credential if writes
are added, rather than expanding the read-only retrieval credential.

Repository access grants remain managed in GitHub. No channel configuration
change should rewrite repository grants or install the App on additional repos.
Specify inherited/nested team access, renames, deleted teams, and retirement
behavior in the synchronization issue. Team sync is the largest operational part
of this phase; it needs reconciliation and recovery, not just a webhook handler.

### Public channel answers

A channel may receive a private repository's answer publicly **within that
channel** when at least one active configured Misty team has a current mapped
GitHub team grant allowing that repository to be read. Merely configuring any
team does not authorize every repository.

```text
Repository A allows GitHub teams A and B
  -> mapped Misty teams A and B
  -> channel has A, B, or both: allow
  -> channel has A and C: allow (A is sufficient)
  -> channel has only C, or no teams: deny shared private-repo retrieval
```

Every repository used in an answer must independently pass the channel check
before search/ranking limits and before content enters model context. Use an
installation credential to fetch channel-authorized evidence. Individual GitHub
sign-in is not required for this channel route. If the channel is not authorized,
offer the requester phase 2's private flow; never automatically disclose their
personal evidence to the channel. Public repository answers remain available
through phase 1 even without channel teams.

This is consistent with document retrieval's **channel audience** model: any user
who can invoke Misty there may ask about the channel's authorized repos. The
requester need not personally belong to the matched team or have GitHub access.
Repository owners must accept this audience policy, and Discord channel owners
must maintain appropriate visibility. Misty team configuration does not itself
change Discord channel permissions. Direct collaborators and organization base
access do not create channel grants unless represented by an approved team grant.

Resolve channel teams through authenticated
[team-tracking HTTP](../services/team-tracking/docs/API.md#channel-team-access).
Use the thread's parent-channel policy already used by the bot, preserve Discord
snowflakes as strings, and use the channel configuration version for cache
invalidation. Reuse the existing
[catalog context conventions](../services/documentation-system/docs/API.md#authorized-retrieval-post-search):
`act-as-channel` with trusted guild/channel assertions, and `act-as-user` for
personal/private requests. GitHub needs its own source-derived grant model;
Google Group checks cannot substitute for GitHub authorization.

### DM access after teams are added

Keep phase 2's individual GitHub authentication for private DMs. Evaluate both
the user's direct access and any grants from their active mapped Misty teams.
These are two possible reasons for access, not a requirement to have both a direct
collaborator grant and a team grant. Someone with direct access can still use a
DM without belonging to a mapped team.

**GitHub's current user-token access remains a required check in either case.**
A Misty team grant is not a bypass around GitHub denying that user access. If
Misty says a user is on a team but GitHub membership sync is pending or revoked,
withhold team-based private evidence until access is confirmed. Conversely, team
retirement or membership removal must invalidate Misty-derived team grants;
separately confirmed direct GitHub access can still permit a phase-2 DM.
A channel's grant never grants the same user DM access by itself.

The access implementation must distinguish direct grants from inherited/team
access where that distinction affects a pending membership revocation. If it
cannot establish a fresh allowed path, fail closed. Document the policy for
unmanaged GitHub teams and outside collaborators before enabling phase 3.

### Suggested implementation issues and acceptance

1. **One-to-one team mapping:** add storage and APIs in team-tracking with stable
   IDs, uniqueness, active-state behavior, permissions, and offline adapter tests.
2. **Team/grant reconciliation:** read GitHub repository-team grants, adopt
   participating teams, define Misty-owned membership sync and its separate write
   permission, handle inheritance, and provide dry runs and recovery procedures.
3. **Channel authorization:** add GitHub-specific grants to retrieval, reuse
   current channel configuration, support public channel delivery, and isolate
   shared evidence from phase-2 personal evidence.
4. **DM team evaluation and revocation:** implement the direct/team path checks,
   invalidation on both providers, and handling of membership-sync disagreement.
5. **Audience and outage tests:** cover the A/B/C cases, a requester without a
   GitHub link in an authorized channel, multiple repositories, threads, team
   retirement, removed grants, stale or partial sync, and history/cache reuse.

Phase 3 is ready when the A/B/C policy works, no personal-only evidence enters a
public reply, and revocation stops further disclosure within the agreed bound.
Never retain grants indefinitely during a sync outage. Previously delivered
Discord messages need a separately agreed retention/removal policy.

## Retrieval decision: MCP on demand, no repository index

**Use GitHub MCP directly for source retrieval in all three phases.** Do not
persist repository bodies, chunks, embeddings, or a symbol index in Misty for
this implementation. “Directly” still means through connectors and the existing
authenticated HTTP boundaries, not a model holding GitHub credentials or calling
unrestricted MCP tools.

Misty builds working context for each question by fetching relevant source files
and passing bounded excerpts to its existing LLM. It therefore has repository
context for that answer, but no independently maintained repository knowledge
base. Follow-up questions retrieve evidence again rather than treating an old
answer as proof of current code. Enrollment, identity mappings, credentials,
team grants, and safe operational metadata still require storage.

| Approach | Benefit | Cost or limitation | Decision |
|---|---|---|---|
| On-demand MCP search, trees, and file reads | No content-ingestion pipeline or persistent source copies; reads can target the requested branch | Repeated API/model work, rate limits, and incomplete retrieval within a budget | Chosen |
| Index code/docs in Misty | Faster repeated searches and possible semantic/symbol retrieval | Content storage, embeddings where used, refresh/prune jobs, authorization-aware retention and recovery | Defer |
| Hybrid retrieval or a dedicated code component | Could improve large-repository or specialized cross-file questions | More moving parts and maintenance; benefits must be measured | Revisit only with evidence; no new Misty service in this plan |

This reduces storage and synchronization work, not necessarily total cost:
repeated on-demand reads can increase API traffic and model input tokens. Begin
with the simpler approach and measure usefulness and spending before revisiting
indexing.

### Branch selection and revision consistency

Users may ask about **any existing branch in an enrolled repository**, including
names containing `/`. If no branch is supplied, resolve GitHub's current default
branch. If the repository or requested branch is ambiguous, ask for clarification;
if the branch is missing, report it instead of silently using a different branch.
Tags and arbitrary historical commits are outside the initial question-selection
UI; commit IDs are still used internally and in citations.

At the start of retrieval, resolve each selected branch to a commit SHA. Use that
commit for all tree and file reads in the answer. If a branch moves while the
answer is generated, finish against the pinned commit and identify it clearly.
A later question resolves the branch again. Explicit branch comparisons may use
two separately identified commits, with evidence kept distinct.

GitHub MCP's `search_code` uses REST code search, which searches the **default
branch**, not arbitrary branches. Treat search hits as candidate paths only and
validate their content at the pinned commit. For a non-default branch, navigate
its pinned tree and read promising files there; default-branch search may provide
additional hints but cannot establish that all relevant files were found. A file
existing only on the requested branch must be discoverable through tree
navigation. Never silently substitute default-branch content. See
[MCP tools](https://github.com/github/github-mcp-server#tools) and
[GitHub code-search restrictions](https://docs.github.com/en/rest/search/search#search-code).

Recursive tree results can be truncated. Inspect the completeness signal and,
within the request budget, fetch subtrees individually. If coverage remains
partial, report the unexamined areas rather than saying there are no matching
files. If the pinned MCP version hides required metadata, extend its adapter or
choose a compatible version before shipping; do not assume a listing is complete.
See [Git Trees completeness](https://docs.github.com/en/rest/git/trees#get-a-tree).

### Finding code, tests, and documentation

Start with a repository tree and, where applicable, scoped lexical code search.
Use the question's terms, file paths, symbol names, imports, and nearby module
names to choose files. Read relevant functions/classes plus enough surrounding
code to interpret them. Follow bounded references into tests and documentation:
for an authorization question, seek both the enforcing code and tests that
exercise it. This is text-based navigation, not a complete call graph or proof
that every possible caller has been inspected.

Use line-numbered excerpts derived from actual fetched text, not model-invented
line locations. Do not embed or run the current catalog text chunker over a
persistent code collection. Symbol-aware parsing may be considered later if the
evaluation shows that bounded lexical navigation is insufficient.

Proposed first enrollment candidate: UTMIST/Misty, after verifying it is public
and a maintainer approves enrollment. Further pilot repositories need explicit
approval. Proposed file coverage for that pilot:

| Material | Initial handling |
|---|---|
| Python, JavaScript/TypeScript, shell, SQL | UTF-8 text reads, including tests; code is never executed |
| Markdown, reStructuredText, plain text | Include documentation and READMEs |
| JSON, YAML, TOML, INI, Dockerfiles | Include relevant configuration, with the same byte limits |
| Notebooks | Exclude initially; do not automatically expose saved outputs |
| Generated/vendor files, lockfiles, minified bundles | Exclude initially; explain when the question needs excluded evidence |
| Binaries, non-UTF-8 files, oversized files | Skip with a coverage reason |
| Submodules and symlinks | Identify but do not follow targets or fetch other repositories |

Language coverage describes source reading, not language-server analysis.
Credential-shaped files such as `.env` and private-key files must be excluded
from fetched context; do not assume that all text in an eligible repo is suitable
to reproduce. This is not a guarantee that source code contains no secrets.

### One question from Discord to an answer

1. The bot authenticates the Misty user and fixes the destination and response
   visibility outside model arguments. Its existing tool loop calls the LLM.
2. The model requests repository search or file evidence. The bot dispatches a
   narrow tool through an authenticated documentation-system HTTP operation.
3. Documentation-system validates enrollment and the public/person/channel
   policy, resolves the branch/commit, and requests bounded source operations
   from connectors. Connectors selects the credential and invokes allowed MCP
   tools. It does not make audience decisions based on model-supplied identities.
4. Retrieval returns excerpts and coverage metadata. The bot appends tool results
   and continues the existing model loop within its budget. There is one model
   tool loop; backend navigation does not start another autonomous agent.
5. Before each model continuation and publication, revalidate the relevant
   access/visibility conditions. Deliver only to the selected audience.

Define the new result contract before implementation: stable repository ID,
current owner/name, requested branch, resolved commit, path, excerpt start/end
lines, content/blob identity, coverage warnings, and citations. Each citation
must link to `https://github.com/{owner}/{repo}/blob/{commit}/{path}#L{start}-L{end}`
with proper path encoding. Safe internal evidence handles can support revalidation;
never return credentials or authorization assertions as model-controlled fields.

The existing LLM API already supports client-executed tools and the bot already
has a bounded tool executor. Extend those; do not introduce a coding agent or
execute source code. Adapt MCP responses into the existing JSON/tool-result
contract and preserve provider continuation blocks. See the
[LLM tool contract](../services/llm/docs/API.md#client-executed-tools).

## Freshness, recovery, and removal

Without a persistent source index there are no source-ingestion checkpoints,
embedding refresh jobs, or stale chunks to prune. Read branch heads anew for
each question, retain fetched source only for the active request, and discard
its evidence and continuation state when the request ends or is cancelled.
Do not place private source/tool results in shared chat history or durable
diagnostic traces. Discord answers already sent remain governed by retention
policy; this design does not promise to retract copies users have read.

Use current metadata and access checks at admission and delivery. On-demand
permission reads may be deduplicated inside one validation pass; a previous
question's permission result is not authorization for a new answer. Phase-3
mirrored grants need a versioned, expiring snapshot and current GitHub validation
of the grants being used. A missing, stale, or incomplete snapshot denies private
team retrieval until reconciliation succeeds. Propose a five-minute maximum
snapshot age for the pilot, plus fresh validation of the relevant GitHub grants
at admission and delivery; test and confirm this policy in the phase-3 sync issue.

File renames and deletions are naturally reflected in newly resolved commits.
If a pinned file/commit becomes unavailable after a force push, report incomplete
evidence rather than switch revisions. Repository renames may update display
paths after validating the stable ID. Transfers out of UTMIST, loss of public
visibility, removal from App access, or unenrollment stop the relevant retrieval
route and invalidate active evidence. Removing a repo also removes its enrolled
branch preferences and derived access records; credential/identity cleanup has
its own owner and retention policy.

Retry transient read failures at most twice with bounded backoff, honoring
GitHub's retry/rate-limit headers and the total deadline. Do not retry permission
denials or missing branches as if they were transient. Stop if a rate-limit wait
would exceed the deadline. Bound retries and subtree work against the same API
budget; cancellation stops issuing further reads. Return explicit statuses for
unavailable, forbidden, not found, budget exhausted, and partial coverage. A
failed listing is not an empty repository. MCP retries must not multiply hidden
provider retries beyond the agreed request budget.

## Initial budgets, operating costs, and evaluation

These are **proposed pilot limits**, to validate and adjust in implementation
issues rather than claims about measured performance. Count every nested provider
read, revalidation, retry, and model continuation; reject oversized work before
downloads or paid calls whenever possible.

| Per-answer limit | Proposed starting point |
|---|---|
| Total deadline, including retrieval and publication checks | 120 seconds |
| Model tool rounds / total model tool calls | 8 / 8 |
| MCP/provider read attempts, including retries and access checks | 32 |
| Source files read / inspected tree entries | 20 / 5,000 |
| Individual source file / total downloaded source text | 256 KiB / 1 MiB |
| One model-visible evidence result | At most 48 KiB of text and within the existing serialized tool-result limit |
| Aggregate model input / generated output across continuations | 40,000 / 8,000 tokens, including repeated context and provider-reported reasoning where applicable |

The bot currently defaults to four tool rounds and four total calls. Changing
GitHub's limits must be explicit, bounded, and tested without silently raising
unrelated tools' budgets. If the provider/tokenizer cannot establish input fits
the remaining budget, reduce evidence or stop before inference. Reserve capacity
for final access checks; do not spend the full request budget on discovery.

Use three planning workloads (50, 500, and 5,000 eligible files per repository)
and 10 versus 100 questions per day. For an illustrative answer using 10 reads,
12,000 aggregate input tokens and 1,000 output tokens, 100 questions/day means
roughly 1,000 reads, 1.2 million input tokens, and 100,000 output tokens per day,
before retries. At the proposed caps the same traffic could reach 3,200 reads,
4 million input tokens, and 800,000 output tokens. These are workload estimates,
not benchmarks or a promise of affordable latency. Larger repos do not raise the
per-question budget automatically; they can produce incomplete answers.

Compute dollar estimates in the rollout issue using the selected model's actual
input/output/reasoning prices, MCP hosting costs, and existing service usage.
No embeddings or persistent code-body storage are budgeted. Track aggregate API
calls, tokens, elapsed time, exhaustion, and error counts without logging private
questions/content. Establish a daily dollar cap and a disable switch before
production rollout; route disabled requests to a clear unavailable response.

Create at least 20 synthetic, offline evaluation questions covering #333's four
examples: authorization plus tests, Discord-to-retrieval flow, local setup, and
documentation versus implementation. Include default/non-default branches,
branch-only files, moving branches, deleted/renamed files, truncated trees,
conflicting docs/code, and missing evidence. Compare README/single-file retrieval
with MCP navigation using the same questions and budgets. Proposed acceptance:

- At least 80% of representative questions have maintainer-reviewed useful
  answers supported by the expected source files; record failures by category.
- Every citation resolves to the examined commit and correct excerpt lines in
  the fake repository; zero fabricated citations or unauthorized evidence.
- Every deliberately incomplete case is identified as incomplete, rather than
  an unsupported assertion that no implementation exists.
- All privacy, prompt-injection boundary, phase-specific access, and revocation
  tests pass. These are gates, not an average quality score.
- Pilot targets: p95 answer latency within 60 seconds and no answer beyond the
  120-second deadline. Check realistic latency in a separate approved deployment
  smoke test; offline fakes alone cannot establish production performance.

Record measured costs and quality against these proposed targets before widening
enrollment. Provide per-phase rollout flags, token/grant failure recovery steps,
and disable procedures. Maintainers should review #333's final decision with
links to this plan, evaluation results, and scoped implementation issues; do not
mark the design complete solely because this overview exists.

## Rules shared by all three phases

- **Read-only, bounded tools:** allow only reviewed navigation/search/file-read
  operations. No repository writes, code execution, installs, hooks, dependency
  downloads, or issue/PR discussion indexing. Team membership management, if
  enabled in phase 3, is a separate administrative process.
- **Untrusted source content:** repository instructions, comments, and files are
  evidence, not authority to change tools, credentials, access, or destinations.
  MCP lockdown mode is defense in depth, not an authorization boundary. See the
  [MCP configuration guide](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md).
- **Revisions and coverage:** resolve a branch to a commit, validate search hits
  against that revision, and cite commit-pinned paths and lines. Bound file bytes,
  search results, tool calls, time, and model spending. Exclude binary/generated/
  vendor files initially and report incomplete scans explicitly.
- **Authorize early and again at delivery:** validate org/repo enrollment,
  audience, and source access before fetching or paid calls where possible.
  Recheck before each publication and cache/history reuse. No private evidence
  may enter an unauthorized model context, even if the final answer hides it.
- **Credentials and audit:** use `SecretStr` settings, unwrap only at provider
  boundaries, encrypt stored credentials, and never log tokens, source content,
  private queries, or private answers. Log actor, operation, status, duration,
  and small safe metadata.
- **Failure behavior:** missing identity, stale permissions, unknown mappings,
  transfers out of UTMIST, revoked App access, and unavailable authorization
  dependencies fail closed for private retrieval. Public retrieval may continue
  only when its independent checks succeed. Revalidate redirects by stable IDs.
- **Service boundaries:** application code depends on Protocols, wiring stays
  centralized, and cross-service calls use authenticated HTTP. Do not share tables
  or vendor clients across services. Preserve existing catalog scope checks.

## Future Decisions

**Repository indexing:** deferred. Revisit an authorization-aware code/document
index or hybrid only if measured retrieval quality, repeated-read spending, or
latency justifies persistent source copies and refresh/removal machinery. Indexing
is not a prerequisite for any of the three phases.

**GitHub issue and PR indexing for RAG:** the current decision is **not to index**
issues, pull request descriptions, comments, or review discussions for
retrieval-augmented generation (RAG). Whether that additional context is useful
enough to include remains open. Revisit this decision if future user feature
requests establish a concrete need, such as answering why a design was chosen.
Any proposal should define source coverage, access checks, freshness, retention,
cost, and evaluation before adding discussion content to retrieval.

**Related features:** other feature choices and scope in this plan may also
change in response to Engineering department requests. Record proposed changes
and their use cases in follow-up issues, review their effect on the three phases
and existing access rules, and update this document when a decision is agreed.
These possible extensions are not commitments or prerequisites for the current
implementation plan.

## Feasibility and issue planning

All three phases are realistic. Phase 1 is the smallest useful release; phase 2
adds OAuth, durable credential handling, and private delivery; phase 3 adds the
most policy and operational complexity through membership/grant reconciliation.
The ownership table defines how this work extends existing Misty services; no
new Misty service is in scope. The required APIs and credential storage still
need to be implemented within those service boundaries.

Create scoped issues per ownership zone from the work packages above, with
explicit cross-service contract dependencies. For each phase, agree on initial
repositories, languages/file types, exact MCP tools/token compatibility,
retrieval-quality questions, latency/cost targets, permission freshness and
revocation bounds, retention, rollout/disable controls, and recovery procedures.
Validate the proposed pilot limits above and settle remaining numerical policies
before enabling production access. Use fake GitHub/MCP/LLM responses and synthetic repos
for tests, never real provider calls. See #333 for the broader evaluation and
design acceptance criteria. This document does not create or close issues.

Documentation scope: this file records the cross-service plan;
[ARCHITECTURE.md](ARCHITECTURE.md) links it from the platform overview. Both changes
belong to the `docs` ownership zone and leave current API/deployment contracts
unchanged.
