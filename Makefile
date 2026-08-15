RUNTIME_DIR := .agents/skills/goodjob-career-review/runtime
FRONTEND_DIR := $(RUNTIME_DIR)/frontend

.PHONY: gate gate-python gate-frontend gate-docs gate-release

gate: gate-python gate-frontend gate-docs

gate-python:
	cd "$(RUNTIME_DIR)" && uv run ruff format --check .
	cd "$(RUNTIME_DIR)" && uv run ruff check .
	cd "$(RUNTIME_DIR)" && uv run mypy .
	cd "$(RUNTIME_DIR)" && uv run mypy --platform win32 .
	cd "$(RUNTIME_DIR)" && uv run pytest -q

gate-frontend:
	cd "$(FRONTEND_DIR)" && npm ci
	cd "$(FRONTEND_DIR)" && npm test

gate-docs:
	uv run --project "$(RUNTIME_DIR)" python -m unittest scripts/test_check_doc_links.py
	uv run --project "$(RUNTIME_DIR)" python scripts/check-doc-links.py

gate-release: gate
	cd "$(FRONTEND_DIR)" && npm run verify
	cd "$(RUNTIME_DIR)" && uv build
