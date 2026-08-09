---
name: Bug report
about: Something is broken or behaves differently than documented
title: ''
labels: bug
assignees: ''
---

Blocked by:

<!-- ^ List the issue numbers this depends on, on that ONE line above, like
     `#40, #42`. The label automation reads them from that line. Delete the line
     entirely if nothing blocks this. -->

## What's wrong

<!-- One or two sentences. -->

## Where

<!-- Delete the ones that don't apply. -->

`discord-bot` · `packages/auth` · `services/connectors` · `services/documentation-system` · `services/llm` · `services/meeting` · `services/team-tracking` · `services/verification` · `docs` · `scripts` · `.github`

**Environment:** local / staging / production

## Steps to reproduce

1.
2.
3.

## Expected

<!-- What should have happened, and where that's documented if it is. -->

## Actual

<!-- What happened. Include the status code, the error, or the log line. -->

```

```

## Notes

<!--
Useful if you have them:
  - Does it reproduce in the web playground (`npm run dev:web`), or only on the real Discord surface?
  - Does the service's `/health` answer? Several services boot fine and only fail on a real call.
  - Anything in the audit log for that request (actor, endpoint, status)?

Please don't paste API keys, one-time codes, or `.env` contents.
-->
