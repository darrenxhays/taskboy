.PHONY: lint format format-check type test check run

lint:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && flake8 agent_harness tests"

format:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && black agent_harness tests spike && isort agent_harness tests spike"

format-check:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && black --check agent_harness tests spike && isort --check agent_harness tests spike"

type:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && mypy agent_harness"

test:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && pytest --disable-pytest-warnings -vv -p no:cacheprovider --show-capture=no"

check: lint type format-check test

run:
	python -m agent_harness.main
