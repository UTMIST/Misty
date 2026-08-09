# UTMIST ops platform — one entry point for the checks CI runs.
#
# Why this exists: verifying a change used to mean eight invocations across
# eight directories, each slightly different (three services take an --ignore
# flag, the bot is npm, packages/auth is neither a service nor the bot). That is
# a lot to remember for a repo with rotating maintainers, and the cost of
# forgetting is a red PR.
#
# `make check` is the whole thing. Run it before you push.
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
.PHONY: help install test test-full lint format check clean-pyc

help: ## Show this help
	@echo 'UTMIST ops platform'
	@echo
	@echo 'Usage: make <target>'
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo 'Single service:  cd services/<name> && uv run pytest'
	@echo 'Full CI list:    .github/workflows/ci.yml'

install: ## Install every dependency (uv workspace + bot)
	uv sync --extra dev
	cd $(BOT_DIR) && npm ci

test: ## Fast tests — no Docker, no network, no credentials
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run pytest -q --ignore=$(PG_SUITE) ); \
	done
	@echo "==> $(BOT_DIR)"
	@cd $(BOT_DIR) && npm test

test-full: ## Every test, including the Postgres adapter suites (needs Docker + migrations)
	@echo "Requires each DB-backed service's Postgres up and migrated."
	@echo "See docs/DEVELOPMENT.md — note documentation-system and verification both bind 5434."
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run pytest -q ); \
	done
	@echo "==> $(BOT_DIR)"
	@cd $(BOT_DIR) && npm test

lint: ## Check lint + formatting everywhere (read-only — same gates as CI)
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run ruff check . && uv run ruff format --check . ); \
	done
	@echo "==> $(BOT_DIR)"
	@cd $(BOT_DIR) && npm run --if-present lint && npm run --if-present format:check

format: ## Apply formatters and autofixes in place
	@set -e; for d in $(PY_DIRS); do \
	  echo "==> $$d"; \
	  ( cd $$d && uv run ruff check --fix . && uv run ruff format . ); \
	done
	@echo "==> $(BOT_DIR)"
	@cd $(BOT_DIR) && npm run --if-present lint:fix && npm run --if-present format

check: lint test ## Lint then test — run this before you push

clean-pyc: ## Remove __pycache__ and stray .pyc files
	find . -path ./.venv -prune -o -name '__pycache__' -type d -print0 2>/dev/null \
	  | xargs -0 rm -rf
