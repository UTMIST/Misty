import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

test('dev-web loads .env and wires LLM settings through initial and reset contexts', () => {
  const src = readFileSync(
    fileURLToPath(new URL('../scripts/dev-web.js', import.meta.url)),
    'utf8',
  );
  assert.match(src, /loadEnvFile/);
  assert.match(src, /llmBaseUrl:\s*requiredEnv\('LLM_BASE_URL'\)/);
  assert.match(src, /llmApiKey:\s*requiredEnv\('LLM_API_KEY'\)/);
  assert.match(src, /createAppContext\(\{\s*\.\.\.config,\s*directoryApiKey:\s*newKey\s*\}\)/s);
});
