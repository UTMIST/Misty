import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';

const FULL = {
  DISCORD_TOKEN: 't',
  DISCORD_CLIENT_ID: 'c',
  DISCORD_GUILD_ID: 'g',
  DIRECTORY_BASE_URL: 'http://localhost:8000/',
  DIRECTORY_API_KEY: 'k',
  DOC_BASE_URL: 'http://localhost:8001/',
  DOC_API_KEY: 'dk',
  LLM_BASE_URL: 'http://localhost:8002/',
  LLM_API_KEY: 'lk',
  VERIFICATION_BASE_URL: 'http://localhost:8003/',
  VERIFICATION_API_KEY: 'vk',
};

test('loadConfig returns typed config and strips trailing slash', () => {
  const cfg = loadConfig(FULL);
  assert.equal(cfg.directoryBaseUrl, 'http://localhost:8000');
  assert.equal(cfg.discordToken, 't');
  assert.equal(cfg.directoryApiKey, 'k');
  assert.equal(cfg.discordGuildId, 'g');
});

test('loadConfig throws listing missing vars', () => {
  assert.throws(() => loadConfig({ DISCORD_TOKEN: 't' }), /DIRECTORY_API_KEY/);
});

test('DISCORD_GUILD_ID is optional (global registration) and not reported missing', () => {
  const { DISCORD_GUILD_ID, ...noGuild } = FULL;
  let cfg;
  assert.doesNotThrow(() => {
    cfg = loadConfig(noGuild);
  });
  assert.equal(cfg.discordGuildId, undefined);
});

test('missing-var error does not list the optional DISCORD_GUILD_ID', () => {
  try {
    loadConfig({ DISCORD_TOKEN: 't' });
    assert.fail('expected loadConfig to throw');
  } catch (err) {
    assert.doesNotMatch(err.message, /DISCORD_GUILD_ID/);
  }
});

test('loadConfig exposes docBaseUrl (slash stripped) and docApiKey', () => {
  const cfg = loadConfig(FULL);
  assert.equal(cfg.docBaseUrl, 'http://localhost:8001');
  assert.equal(cfg.docApiKey, 'dk');
});

test('loadConfig throws when DOC_BASE_URL missing', () => {
  const { DOC_BASE_URL, ...rest } = FULL;
  assert.throws(() => loadConfig(rest), /DOC_BASE_URL/);
});

test('loadConfig requires LLM_BASE_URL and LLM_API_KEY', () => {
  const base = {
    DISCORD_TOKEN: 't',
    DISCORD_CLIENT_ID: 'c',
    DIRECTORY_BASE_URL: 'http://d',
    DIRECTORY_API_KEY: 'dk',
    DOC_BASE_URL: 'http://doc',
    DOC_API_KEY: 'dock',
    VERIFICATION_BASE_URL: 'http://v',
    VERIFICATION_API_KEY: 'vk',
  };
  assert.throws(() => loadConfig(base), /LLM_BASE_URL/);
});

test('loadConfig exposes llmBaseUrl (trailing slash stripped) and llmApiKey', () => {
  const cfg = loadConfig({
    DISCORD_TOKEN: 't',
    DISCORD_CLIENT_ID: 'c',
    DIRECTORY_BASE_URL: 'http://d',
    DIRECTORY_API_KEY: 'dk',
    DOC_BASE_URL: 'http://doc',
    DOC_API_KEY: 'dock',
    LLM_BASE_URL: 'http://llm.railway.internal:8000/',
    LLM_API_KEY: 'llmkey',
    VERIFICATION_BASE_URL: 'http://v',
    VERIFICATION_API_KEY: 'vk',
  });
  assert.equal(cfg.llmBaseUrl, 'http://llm.railway.internal:8000');
  assert.equal(cfg.llmApiKey, 'llmkey');
});

test('loadConfig requires VERIFICATION_BASE_URL and VERIFICATION_API_KEY', () => {
  const base = {
    DISCORD_TOKEN: 't',
    DISCORD_CLIENT_ID: 'c',
    DIRECTORY_BASE_URL: 'http://d',
    DIRECTORY_API_KEY: 'dk',
    DOC_BASE_URL: 'http://doc',
    DOC_API_KEY: 'dock',
    LLM_BASE_URL: 'http://llm',
    LLM_API_KEY: 'lk',
  };
  assert.throws(() => loadConfig(base), /VERIFICATION_BASE_URL/);
});

test('loadConfig exposes verificationBaseUrl (trailing slash stripped) and verificationApiKey', () => {
  const cfg = loadConfig({
    DISCORD_TOKEN: 't',
    DISCORD_CLIENT_ID: 'c',
    DIRECTORY_BASE_URL: 'http://d',
    DIRECTORY_API_KEY: 'dk',
    DOC_BASE_URL: 'http://doc',
    DOC_API_KEY: 'dock',
    LLM_BASE_URL: 'http://llm',
    LLM_API_KEY: 'lk',
    VERIFICATION_BASE_URL: 'http://verify.railway.internal:8000/',
    VERIFICATION_API_KEY: 'verifykey',
  });
  assert.equal(cfg.verificationBaseUrl, 'http://verify.railway.internal:8000');
  assert.equal(cfg.verificationApiKey, 'verifykey');
});
