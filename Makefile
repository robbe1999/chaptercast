.PHONY: help setup dev-api dev-web test test-api test-web lint typecheck scan openapi build docker check

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Install backend and frontend dependencies
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
	cd frontend && npm ci

dev-api:  ## Run the API with auto-reload (demo mode unless a key is in .env)
	# Run from the repo root so the root .env is picked up (settings read ./.env).
	. backend/.venv/bin/activate && uvicorn chaptercast.main:create_app --factory --reload --reload-dir backend/chaptercast --port 8000

dev-web:  ## Run the frontend dev server (proxies /api to :8000)
	cd frontend && npm run dev

test: test-api test-web  ## Run all tests

test-api:  ## Backend tests with coverage
	cd backend && . .venv/bin/activate && pytest

test-web:  ## Frontend tests
	cd frontend && npm test

lint:  ## Lint everything
	cd backend && . .venv/bin/activate && ruff check . && ruff format --check .
	cd frontend && npm run lint

typecheck:  ## Strict type checks
	cd backend && . .venv/bin/activate && mypy
	cd frontend && npm run typecheck

scan:  ## Scan the working tree for committed secrets
	python3 scripts/scan_secrets.py

openapi:  ## Regenerate docs/openapi.json after an API change
	cd backend && . .venv/bin/activate && CHAPTERCAST_UPDATE_SNAPSHOT=1 pytest tests/test_contracts.py --no-cov -q

build:  ## Production frontend build
	cd frontend && npm run build

docker:  ## Build and run in a hardened container
	docker compose up --build

check: lint typecheck test scan  ## Everything CI runs
