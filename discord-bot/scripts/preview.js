import { createInterface } from 'node:readline/promises';
import { REPOSITORY, switchPreview } from './lib/preview.js';
import { jsonCommand, parsePreviewArgs, railwayApi, readPreviewSource } from './lib/previewCli.js';

async function main() {
  const { number, projectId, planOnly, confirmedIdle, timeoutMs } = parsePreviewArgs(
    process.argv.slice(2),
  );
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
    readSource: readPreviewSource,
    api: railwayApi,
    timeoutMs,
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
