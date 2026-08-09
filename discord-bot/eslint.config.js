// Flat config (ESLint 9). The bot is ESM ("type": "module") on Node 20+.
//
// Scope note: this lints the bot only. The Python services are gated by ruff —
// see the per-service CI jobs in .github/workflows/ci.yml.
//
// eslint-config-prettier must stay LAST: it turns off every stylistic rule that
// would otherwise fight `prettier --check`. Formatting is Prettier's job, not
// ESLint's; ESLint here is only for correctness bugs.

import js from '@eslint/js';
import globals from 'globals';
import prettier from 'eslint-config-prettier';

export default [
  { ignores: ['node_modules/**'] },

  js.configs.recommended,

  {
    files: ['**/*.js'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.node },
    },
    rules: {
      // `_`-prefixed args are the repo's existing convention for deliberately
      // unused params (e.g. the `(_m, s) =>` replacer in src/config.js).
      //
      // ignoreRestSiblings allows the omit idiom the codebase already uses to
      // strip a key: `const { flags, ...editable } = dpayload`. The named
      // sibling is the point of the destructure, not dead code.
      'no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          ignoreRestSiblings: true,
        },
      ],
    },
  },

  {
    // Browser-side playground assets — served to the page, not run under Node.
    files: ['src/web/public/**/*.js'],
    languageOptions: { globals: { ...globals.browser } },
  },

  prettier,
];
