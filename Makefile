# Local mirror of the CI `quality` job. Running `make check` before pushing should give
# the same answer CI does; that is the whole point of it existing (handbook 34).
#
# Windows: the `python` command is often a Microsoft Store stub. Use `make PYTHON=py check`.
PYTHON ?= python

.PHONY: help policy governance lint format typecheck test check

help:
	@echo "policy     - validate the coding agent policy against the Constitution"
	@echo "governance - validate the repository governance manifest against this tree"
	@echo "lint       - ruff lint and format check"
	@echo "format     - apply ruff formatting"
	@echo "typecheck  - mypy"
	@echo "test       - pytest"
	@echo "check      - everything CI runs, in the same order"

policy:
	$(PYTHON) scripts/validate_coding_agent_policy.py

governance:
	$(PYTHON) scripts/validate_repository_governance.py

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .

typecheck:
	$(PYTHON) -m mypy scripts tests

test:
	$(PYTHON) -m pytest -q

check: policy governance lint typecheck test
