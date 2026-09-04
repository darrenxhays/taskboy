.PHONY: lint format format-check type test check run

export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

# python -m: pip installs to a user bin dir that isn't on PATH when the container runs as the host user
lint:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && python -m flake8 taskboy tests"

format:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && python -m black taskboy tests spike && python -m isort taskboy tests spike"

format-check:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && python -m black --check taskboy tests spike && python -m isort --check taskboy tests spike"

type:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && python -m mypy taskboy"

test:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && python -m pytest --disable-pytest-warnings -vv -p no:cacheprovider --show-capture=no"

check: lint type format-check test

run:
	python -m taskboy.main
