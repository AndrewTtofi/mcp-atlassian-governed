.PHONY: install up down logs test lint fmt lock audit

install:
	pip install --require-hashes -r requirements-dev.txt

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f mcp

test:
	pytest -q

lint:
	ruff check app tests && ruff format --check app tests && mypy app

fmt:
	ruff format app tests && ruff check --fix app tests

# Re-pin after editing requirements*.in (ARGS=--upgrade to bump everything).
PIP_COMPILE = pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe --no-emit-index-url
lock:
	$(PIP_COMPILE) $(ARGS) -o requirements.txt requirements.in
	$(PIP_COMPILE) $(ARGS) -o requirements-dev.txt requirements-dev.in

audit:
	pip-audit -r requirements.txt --no-deps --disable-pip --progress-spinner off
