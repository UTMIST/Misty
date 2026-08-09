# UTMIST ops platform — one entry point for the checks CI runs.
#
# Why this exists: verifying a change used to mean eight invocations across
# eight directories, each slightly different (three services take an --ignore
# flag, the bot is npm, packages/auth is neither a service nor the bot). That is
# a lot to remember for a repo with rotating maintainers, and the cost of
# forgetting is a red PR.
#
# `make check` covers most changes. It runs the FAST suites — it does NOT run
# the Postgres-adapter tests CI runs (65 of them across the three DB-backed
# services, plus 3 gated behind RUN_PG_TESTS). So a green `make check` is not
# proof of a green CI: if you touched storage, an adapter, or a migration, run
# `make test-full` with Postgres up before pushing.
#
# This is a convenience wrapper, NOT the source of truth.
# .github/workflows/ci.yml is authoritative — if the two ever disagree, CI wins
# and this file is the bug. See AGENTS.md → "Run before you push".

# uv and npm both resolve their own toolchains, so there is nothing to activate.
PY_DIRS := services/team-tracking \
           services/documentation-system \
           services/verification \
           services/llm \
           services/meeting \
           services/connectors \
           packages/auth

BOT_DIR := discord-bot

# Only team-tracking, documentation-system and verification have a Postgres
# adapter suite. Passing --ignore for a path that does not exist is a harmless
# no-op in pytest, so the same flag works across all seven directories.
PG_SUITE := tests/test_postgres_adapter.py

.DEFAULT_GOAL := help
.PHONY: help install test test-full lint format check check-node clean-pyc

help: ## Show this help
	@echo 'UTMIST ops platform'
	@echo
	@echo 'Usage: make <target>'
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo 'make check runs the FAST suites. It does not run the Postgres-adapter'
	@echo 'tests that CI does — if you touched storage, an adapter, or a'
	@echo 'migration, run make test-full before pushing.'
	@echo
	@echo 'First run:       make install  (without it, uv run ruff has no ruff)'
	@echo 'Single service:  cd services/<name> && uv run pytest'
	@echo 'Full CI list:    .github/workflows/ci.yml'

# The bot's failure mode on an old Node is unrecognisable — `node:test` reports
# no `mock` export, fastify wants `diagnostics.tracingChannel` — and neither
# message mentions Node. Check the declared floor first and say so plainly.
# Reads engines from package.json so there is only one place to bump.
check-node:
	@command -v node >/dev/null 2>&1 || { \
	  echo ''; \
	  echo '  node not found. See docs/DEVELOPMENT.md -> Prerequisites.'; \
	  echo ''; exit 1; }
	@node -e 'var need=require("./$(BOT_DIR)/package.json").engines.node; \
	var m=need.replace(/[^0-9.]/g,"").split(".").map(Number); \
	var c=process.versions.node.split(".").map(Number); \
	var ok=c[0]>m[0]||(c[0]===m[0]&&(c[1]||0)>=(m[1]||0)); \
	if(!ok){ \
	  console.error("\n  The bot needs Node "+need+" (from $(BOT_DIR)/package.json engines)."); \
	  console.error("  You are on v"+process.versions.node+", so its tests and lint will fail"); \
	  console.error("  in ways that do not mention Node at all.\n"); \
	  console.error("  Fix: nvm install "+m[0]+" && nvm use "+m[0]+"\n"); \
	  process.exit(1); }'

install: ## Install every dependency (uv workspace + bot)
	uv sync --extra dev
	cd $(BOT_DIR) && npm ci

test: ## Fast suites only — no Docker, network, or credentials (see test-full)
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run pytest -q --ignore=$(PG_SUITE) ); \
	done
	@echo "==> $(BOT_DIR)"
	@$(MAKE) --no-print-directory check-node
	@cd $(BOT_DIR) && npm test

test-full: ## Every test, including the Postgres adapter suites (needs Docker + migrations)
	@echo "Requires each DB-backed service's Postgres up and migrated."
	@echo "See docs/DEVELOPMENT.md — note documentation-system and verification both bind 5434."
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run pytest -q ); \
	done
	@echo "==> $(BOT_DIR)"
	@$(MAKE) --no-print-directory check-node
	@cd $(BOT_DIR) && npm test

lint: ## Check lint + formatting everywhere (read-only — same gates as CI)
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run ruff check . && uv run ruff format --check . ); \
	done
	@echo "==> $(BOT_DIR)"
	@$(MAKE) --no-print-directory check-node
	@cd $(BOT_DIR) && npm run --if-present lint && npm run --if-present format:check

format: ## Apply formatters and autofixes in place
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run ruff check --fix . && uv run ruff format . ); \
	done
	@echo "==> $(BOT_DIR)"
	@$(MAKE) --no-print-directory check-node
	@cd $(BOT_DIR) && npm run --if-present lint:fix && npm run --if-present format

check: lint test ## Lint + fast tests. Pre-push for most changes; see test-full

clean-pyc: ## Remove __pycache__ and stray .pyc files
	find . -path ./.venv -prune -o -name '__pycache__' -type d -print0 2>/dev/null \
	  | xargs -0 rm -rf
