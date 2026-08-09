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

  // Rules apply everywhere. Kept separate from the globals blocks below so the
  // two runtime environments can be mutually exclusive without duplicating them.
  {
    files: ['**/*.js'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
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

  // Globals are split into two NON-overlapping blocks on purpose. Flat config
  // *merges* languageOptions.globals across every block whose `files` match, so
  // a browser block layered on top of a `**/*.js` node block would hand browser
  // files both sets — and `process.env.X` in playground code would lint clean
  // and then break in the page. `ignores` here is what keeps them disjoint.
  {
    files: ['**/*.js'],
    ignores: ['src/web/public/**'],
    languageOptions: { globals: { ...globals.node } },
  },

  {
    // Browser-side playground assets — served to the page, not run under Node.
    files: ['src/web/public/**/*.js'],
    languageOptions: { globals: { ...globals.browser } },
  },

  prettier,
];
