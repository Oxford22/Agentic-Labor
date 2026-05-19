.PHONY: install lint format type test e2e guardrails ci clean

PYTHON ?= python3
UV ?= uv

install:
	$(UV) sync --all-extras --dev

lint:
	$(UV) run ruff check packages tests scripts

format:
	$(UV) run ruff format packages tests scripts

format-check:
	$(UV) run ruff format --check packages tests scripts

type:
	$(UV) run mypy packages/putsch_contracts/src

test:
	$(UV) run pytest packages tests -v

e2e:
	$(UV) run pytest tests/integration -v -m e2e

guardrails:
	$(PYTHON) scripts/check_forbidden_deps.py
	$(PYTHON) scripts/check_workflow_residency.py
	$(PYTHON) scripts/check_contracts_imported.py

ci: lint format-check type test guardrails

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
