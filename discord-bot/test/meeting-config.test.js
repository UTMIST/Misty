import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { loadConfig } from '../src/config.js';

test('meeting config: base url and ws url derivation', async (t) => {
  await t.test('meetingBaseUrl strips trailing slash', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
      MEETING_BASE_URL: 'http://meeting.railway.internal:8003/',
    });
    assert.equal(cfg.meetingBaseUrl, 'http://meeting.railway.internal:8003');
  });

  await t.test('meetingWsUrl derives ws:// from http://', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
      MEETING_BASE_URL: 'http://meeting.railway.internal:8003',
    });
    assert.equal(cfg.meetingWsUrl, 'ws://meeting.railway.internal:8003');
  });

  await t.test('meetingWsUrl derives wss:// from https://', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
      MEETING_BASE_URL: 'https://meeting.railway.internal:8003/',
    });
    assert.equal(cfg.meetingWsUrl, 'wss://meeting.railway.internal:8003');
  });

  await t.test('meetingBaseUrl and meetingWsUrl are undefined when not set', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
    });
    assert.equal(cfg.meetingBaseUrl, undefined);
    assert.equal(cfg.meetingWsUrl, undefined);
  });

  await t.test('meetingApiKey from MEETING_API_KEY env var', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
      MEETING_API_KEY: 'test-key',
    });
    assert.equal(cfg.meetingApiKey, 'test-key');
  });

  await t.test('meetingApiKey is undefined when not set', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
    });
    assert.equal(cfg.meetingApiKey, undefined);
  });

  await t.test('meetingWsUrl uses MEETING_WS_URL when explicitly set', () => {
    const cfg = loadConfig({
      DISCORD_TOKEN: 'test',
      DISCORD_CLIENT_ID: 'test',
      DIRECTORY_BASE_URL: 'http://localhost',
      DIRECTORY_API_KEY: 'test',
      DOC_BASE_URL: 'http://localhost',
      DOC_API_KEY: 'test',
      LLM_BASE_URL: 'http://localhost',
      LLM_API_KEY: 'test',
      VERIFICATION_BASE_URL: 'http://localhost',
      VERIFICATION_API_KEY: 'test',
      MEETING_BASE_URL: 'http://meeting.railway.internal:8003',
      MEETING_WS_URL: 'wss://custom.ws.url',
    });
    assert.equal(cfg.meetingWsUrl, 'wss://custom.ws.url');
  });
});
