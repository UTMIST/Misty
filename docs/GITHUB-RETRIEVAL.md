# GitHub repository retrieval: MCP and access design

Design notes for [#333](https://github.com/UTMIST/Misty/issues/333), covering
read-only Q&A over source code and documentation. GitHub MCP is the planned
retrieval interface. The policies below are proposed for review; they are not
implemented controls or a completed architecture decision for #333.

Today, connectors fetches one explicitly supplied README or text file through
the REST API and omits GitHub permissions. See the existing
[fetch contract](../services/connectors/docs/API.md#post-fetch).
Successful fetching does not establish who may receive the content.

## Proposed first release

Start with explicitly enrolled public UTMIST repositories, one selected branch
per repository, and bounded on-demand navigation and file reads through MCP.
Members do not need to link GitHub accounts to ask about public source content.
Use a backend credential for MCP authentication. Add private repositories only
after verified account linking and user authorization are implemented; initially
deliver private answers only in DMs to the authorized requester.

MCP supplies tools; Misty still owns repository enrollment, authorization,
answer destinations, revision consistency, and resource limits. An MCP connection
does not replace those checks or guarantee complete repository retrieval.

## Organization-owned GitHub App

Register a dedicated GitHub App under the **UTMIST organization**, choosing
**Only on this account** for installation availability. This makes it a private
App, installable only on its owning account. An App owned by a personal account
would instead be restricted to that personal account. Install the App on selected
repositories and grant repository **Contents: read** and the required metadata
access. Do not request repository writes or organization membership permissions
for this first slice. See GitHub's
[App installation availability](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private).

Installation availability and repository visibility are different controls.
GitHub Apps can still read public repositories elsewhere on GitHub. Misty must
enforce its own enrolled-repository allowlist, including on search operations.
Use stable organization and repository IDs, and revalidate enrollment on
transfers or ownership changes; following a redirect must not widen access.
See [GitHub App access](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party).

## Public and private authentication

| Retrieval case | GitHub authentication and member linking |
|---|---|
| Public files through REST endpoints supporting anonymous access | No GitHub sign-in is required. The basic unauthenticated REST limit is 60 requests per hour per IP; some endpoints require authentication. |
| Public files through the official GitHub MCP server | MCP requires a GitHub token. A backend credential is sufficient; members need no GitHub login or directory link. |
| Private files for a member | Require a verified GitHub link and user authorization. Prefer GitHub App user access tokens, which limit access to resources both the user and the App can access. |
| Future background indexing | Installation tokens may fetch enrolled repositories, but their access does not prove any member or channel may receive the indexed content. |

Sources: [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api),
[MCP authentication](https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md#authentication-and-authorization),
[GitHub App user authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user),
and [installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

Public repository access does not remove Misty's own authenticated HTTP
boundaries or the person/channel requirement when using documentation-system
search. Preserve its
[retrieval contract](../services/documentation-system/docs/API.md#authorized-retrieval-post-search).
Before reusing a cached public result, revalidate visibility under a defined
freshness policy; loss of public visibility must stop anonymous disclosure.

## Verified member identity and credentials

Team-tracking already supports a `github` provider through
[person identifiers](../services/team-tracking/docs/API.md#person-identifiers).
The proposed private-repository flow must prove account ownership through GitHub
authorization, bound to the authenticated Misty person. A manually entered
username is insufficient. Record the stable numeric GitHub account ID alongside
the current login so account renames do not change the identity binding.
That representation and migration still need design before implementation.

Keep identity associations in team-tracking. Keep source tokens, refresh tokens,
and App keys behind the connector/provider boundary, out of directory records
and model context. Any durable token store needs an explicit owner and encrypted
storage design; connectors currently has no database. Credential settings use
`SecretStr`, unwrapped only at their use boundary. Logs must contain neither
credentials nor private source content.

For interactive private reads, obtain a GitHub App user access token after the
member authorizes the App. GitHub enforces both user access and App permissions,
including installation access to the organization. Linking does not grant the
member new repository access. Misty must also verify that the person remains
active and the repository remains enrolled. Directory team membership alone is
not evidence of GitHub repository access.

## Required restrictions

- **Tools:** enable MCP read-only mode and an explicit allowlist of navigation,
  code-search, and file-read tools. Exclude write operations, Actions execution,
  and unrelated tools. Issue/PR discussion retrieval is outside the first slice.
  Backend configuration fixes the tool set and MCP endpoint; model arguments
  cannot change them. Review the tool inventory when upgrading the server.
- **Repository scope:** validate every tool call against enrolled repositories
  before fetching. Constrain search queries server-side and validate returned
  repository identities before content enters model context. Never use a shared
  maintainer PAT as a substitute for member authorization.
- **Authenticated context:** derive the person and answer destination outside
  model arguments. Select credentials server-side for that person. Reject any
  attempt to choose another person, token, or installation through tool inputs.
- **Destinations:** private retrieval initially supports authorized DMs only.
  A requester with repository access cannot publish its contents into a shared
  channel through Misty. Channel support requires a reviewed repository-to-channel
  audience policy and authorization before retrieval and publication.
- **Untrusted evidence:** code, comments, and repository instructions cannot
  change permissions, authorize actions, or redirect credentials. Never execute
  repository code, install dependencies, or run hooks during retrieval. MCP
  lockdown mode is optional defense in depth; it is a best-effort public-content
  filter and does not establish private-repository authorization.
- **Bounds:** validate limits before downloads or paid calls where possible.
  Bound file bytes, returned results, tool calls, wall time, and model usage.
  Initially exclude binaries, generated/vendor files, dependency downloads,
  submodule traversal, and full history search. Report truncation and incomplete
  listings explicitly.
- **Revision evidence:** resolve the selected branch to a commit and fetch
  supporting files at that commit. Scope any search results to that revision or
  validate them with commit-pinned reads. Cite paths and lines at the examined
  commit; distinguish documentation claims from code behavior and identify gaps.
- **Caches and revocation:** check current authorization before serving private
  cached or indexed content. Include repository ID and commit in content keys,
  and isolate user/destination authorization and conversation history. Recheck
  before publication; fail closed when authorization cannot be established.
  Unlinking, offboarding, App removal, and repository removal must disable further
  disclosure and trigger the agreed cleanup of stored and derived data. Previously
  delivered Discord messages require a separate retention/removal policy.

MCP supports individual tool selection and read-only filtering; lockdown mode
does not restrict the underlying credential's permissions. See the
[server configuration guide](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md).

## Remaining design work and verification

Before implementation, agree on initial repositories and languages, the exact
tool inventory and its support for the chosen token types, numerical budgets,
visibility/access freshness, retention and removal policy, and offline quality
and latency targets. Compare the MCP approach with the current single-file
baseline using #333's representative questions. Indexing remains a later option;
this proposal does not require embeddings, a new service, or a full code index.

Tests must use synthetic repositories and fake GitHub/MCP/LLM responses. Cover
public retrieval without member linking, unenrolled repositories, private access
isolation, revoked access, account/repository renames and transfers, shared-channel
denials, prompt injection, cross-user cache/history reuse, branch movement,
commit citations, and partial or over-budget retrieval. No test makes live GitHub
or LLM calls. Record maintainer review and follow-up implementation tasks in
#333; these notes do not close its broader design acceptance criteria.

## Documentation change scope

- `docs/GITHUB-RETRIEVAL.md` records the proposed cross-service MCP and access
  model for #333 without changing current endpoint or deployment contracts.
- `docs/ARCHITECTURE.md` links these design notes so contributors can find them
  from the platform overview. Both files remain in the `docs` ownership zone.
