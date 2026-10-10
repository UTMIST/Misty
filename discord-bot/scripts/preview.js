import { createInterface } from 'node:readline/promises';
import { REPOSITORY, switchPreview } from './lib/preview.js';
import { jsonCommand, railwayApi } from './lib/previewCli.js';

// Public project identifier; credentials still come from Railway CLI login.
const DEFAULT_PROJECT_ID = 'abf9e8a2-9394-48e2-9d95-a71520c58a5b';

async function main() {
  const args = process.argv.slice(2);
  const number = args.shift();
  let projectId = process.env.MISTY_PREVIEW_PROJECT_ID || DEFAULT_PROJECT_ID;
  let planOnly = false;
  let confirmedIdle = false;
  while (args.length) {
    const arg = args.shift();
    if (arg === '--project' && args[0]) projectId = args.shift();
    else if (arg === '--plan') planOnly = true;
    else if (arg === '--recordings-stopped') confirmedIdle = true;
    else throw new Error(`Unknown or incomplete option: ${arg}`);
  }
  if (!/^[1-9]\d*$/.test(number ?? '') || !projectId) {
    throw new Error(
      'Usage: npm run preview -- <pr> [--project <Railway-project-id>] [--plan] [--recordings-stopped]',
    );
  }
  const pr = await jsonCommand('gh', [
    'pr',
    'view',
    number,
    '--repo',
    REPOSITORY,
    '--json',
    'number,state,baseRefName,headRefOid,isCrossRepository',
  ]);
  const result = await switchPreview({
    projectId,
    pr,
    api: railwayApi,
    confirm: async () => {
      if (planOnly) return false;
      if (confirmedIdle) return true;
      if (!process.stdin.isTTY) {
        throw new Error('Stop recordings, wait for minutes, then pass --recordings-stopped.');
      }
      const prompt = createInterface({ input: process.stdin, output: process.stdout });
      try {
        const answer = await prompt.question(
          'Switching disconnects the dev bot and restarts its backends. Have recordings finished and minutes arrived? [y/N] ',
        );
        return /^y(es)?$/i.test(answer.trim());
      } finally {
        prompt.close();
      }
    },
  });
  if (result.changed) console.log(`dev is running PR #${number} (${result.commitSha}).`);
  else console.log('No deployments changed.');
}

main().catch((err) => {
  console.error(err.message);
  process.exitCode = 1;
});
