import { test } from 'node:test';
import assert from 'node:assert/strict';
import bug, { BUG_REPORT_URL, formatContact, buildBugReportMessage } from '../src/commands/bug.js';
import { commands } from '../src/commands/index.js';

test('/bug is public, stable, and registered', () => {
  assert.equal(bug.auth, 'public');
  assert.equal(bug.beta, false);
  assert.equal(bug.description, 'Where to report a bug');
  assert.equal(commands.get('bug'), bug);
});

test('/bug returns only the GitHub issue link when infrastructure contact is unset', async () => {
  const payload = await bug.handler({
    ctx: { infrastructureDiscordUsername: undefined },
  });

  assert.equal(payload.ephemeral, true);
  assert.equal(payload.content, `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}`);
  assert.doesNotMatch(payload.content, /reach out/);
});

test('/bug returns the GitHub issue link and configured infrastructure username', async () => {
  const payload = await bug.handler({
    ctx: { infrastructureDiscordUsername: 'infra-user' },
  });

  assert.equal(payload.ephemeral, true);
  assert.ok(payload.content.includes(BUG_REPORT_URL));
  assert.match(payload.content, /@infra-user/);
  assert.ok(
    payload.content.includes(
      `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}\nOr reach out to @infra-user on Discord.`,
    ),
  );
});

test('/bug formats contact as a Discord mention when given a numeric snowflake user ID', async () => {
  const payload = await bug.handler({
    ctx: { infrastructureDiscordUsername: '123456789012345678' },
  });

  assert.equal(payload.ephemeral, true);
  assert.ok(payload.content.includes(BUG_REPORT_URL));
  assert.ok(payload.content.includes('Or reach out to <@123456789012345678> on Discord.'));
});

test('formatContact handles plain handles, leading @, and snowflake IDs', () => {
  assert.equal(formatContact('infra-user'), '@infra-user');
  assert.equal(formatContact('@infra-user'), '@infra-user');
  assert.equal(formatContact('123456789012345678'), '<@123456789012345678>');
});

test('buildBugReportMessage returns base link when contact is omitted or blank', () => {
  assert.equal(buildBugReportMessage(), `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}`);
  assert.equal(buildBugReportMessage(''), `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}`);
  assert.equal(buildBugReportMessage('   '), `Found a bug? Open a GitHub issue: ${BUG_REPORT_URL}`);
});
