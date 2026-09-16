.DEFAULT_GOAL := help
SHELL := /bin/bash

PY ?= .venv/Scripts/python.exe
ifeq (,$(wildcard $(PY)))
PY := .venv/bin/python
endif
ifeq (,$(wildcard $(PY)))
PY := python
endif

# Integration tests reach Postgres on the host, as the unprivileged app role.
export TEST_DATABASE_URL ?= postgresql+asyncpg://app_rw:app_rw@localhost:5432/agentdb
export TEST_OWNER_DATABASE_URL ?= postgresql+asyncpg://app:app@localhost:5432/agentdb

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup -------------------------------------------------------------------

.PHONY: install
install:  ## Create a virtualenv and install the project
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

.PHONY: env
env:  ## Copy .env.example to .env if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env -- review OLLAMA_BASE_URL")

# --- stack -------------------------------------------------------------------

.PHONY: up
up: env  ## Start the full stack (api, worker, postgres, redis)
	docker compose up -d --build
	@echo "API: http://localhost:8000/docs"

.PHONY: services
services:  ## Start only Postgres and Redis (for running the app locally)
	docker compose up -d postgres redis

.PHONY: down
down:  ## Stop the stack, keeping volumes
	docker compose down

.PHONY: clean
clean:  ## Stop the stack and delete all data
	docker compose down -v

.PHONY: logs
logs:  ## Follow api and worker logs
	docker compose logs -f api worker

.PHONY: ps
ps:  ## Show service status
	docker compose ps

# --- database ----------------------------------------------------------------

.PHONY: migrate
migrate:  ## Apply migrations (as the owner role)
	DATABASE_URL=$(TEST_OWNER_DATABASE_URL) $(PY) -m alembic upgrade head

.PHONY: migration
migration:  ## Generate a migration: make migration m="add x"
	DATABASE_URL=$(TEST_OWNER_DATABASE_URL) $(PY) -m alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade:  ## Roll back one migration
	DATABASE_URL=$(TEST_OWNER_DATABASE_URL) $(PY) -m alembic downgrade -1

# Seeding runs INSIDE the api container, as the owner role. Two reasons:
# the `postgres` hostname only resolves on the compose network, and creating an
# organization is deliberately outside what app_rw can do -- the policy on
# `organizations` restricts a session to its own row, so there is no tenant
# context under which a new tenant can be inserted.
OWNER_URL_IN_COMPOSE = postgresql+asyncpg://app:app@postgres:5432/agentdb

.PHONY: seed
seed:  ## Seed the Sagar Hotels demo tenant (runs in the api container)
	docker compose exec -T -e DATABASE_URL="$(OWNER_URL_IN_COMPOSE)" api python -m scripts.seed_demo

.PHONY: reseed
reseed:  ## Delete and re-create the demo tenant
	docker compose exec -T -e DATABASE_URL="$(OWNER_URL_IN_COMPOSE)" api python -m scripts.seed_demo --reset

.PHONY: owner
owner:  ## Create a platform operator: make owner email=you@example.com
	docker compose exec -T -e DATABASE_URL="$(OWNER_URL_IN_COMPOSE)" api python -m scripts.create_owner --email "$(email)"

.PHONY: owners
owners:  ## List platform operators
	docker compose exec -T -e DATABASE_URL="$(OWNER_URL_IN_COMPOSE)" api python -m scripts.create_owner --list

.PHONY: seed-local
seed-local:  ## Seed when running the app on the host (make services + make api)
	DATABASE_URL="postgresql+asyncpg://app:app@localhost:5432/agentdb" $(PY) -m scripts.seed_demo

# --- web console ---------------------------------------------------------------

.PHONY: web-install
web-install:  ## Install the web console's dependencies
	cd web && npm install

.PHONY: web
web:  ## Run the web console (needs the API up: make up)
	cd web && npm run dev

.PHONY: web-check
web-check:  ## Typecheck the web console
	cd web && npx tsc --noEmit

# --- running -----------------------------------------------------------------

.PHONY: api
api:  ## Run the API locally with reload
	$(PY) -m uvicorn app.main:app --reload --port 8000

.PHONY: worker
worker:  ## Run an ingestion worker locally
	$(PY) -m app.workers.runner

# --- quality -----------------------------------------------------------------

.PHONY: test
test:  ## Run unit tests only (no services needed)
	$(PY) -m pytest tests/unit -q

.PHONY: test-integration
test-integration:  ## Run integration tests (needs postgres + redis)
	$(PY) -m pytest tests/integration -q

.PHONY: test-e2e
test-e2e:  ## Run end-to-end tests (needs postgres + redis)
	$(PY) -m pytest tests/e2e -q

.PHONY: test-all
test-all:  ## Run everything
	$(PY) -m pytest tests -q

.PHONY: cov
cov:  ## Run the suite with a coverage report
	$(PY) -m pytest tests --cov=app --cov-report=term-missing -q

.PHONY: lint
lint:  ## Lint and type-check
	$(PY) -m ruff check app tests scripts
	$(PY) -m mypy app

.PHONY: fmt
fmt:  ## Format and auto-fix
	$(PY) -m ruff check app tests scripts --fix
	$(PY) -m ruff format app tests scripts

.PHONY: check
check: lint test  ## Lint plus unit tests -- the pre-commit gate
