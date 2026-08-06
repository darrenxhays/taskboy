.PHONY: lint format format-check type test check run

lint:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && flake8 taskboy tests"

format:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && black taskboy tests spike && isort taskboy tests spike"

format-check:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && black --check taskboy tests spike && isort --check taskboy tests spike"

type:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && mypy taskboy"

test:
	docker compose run --rm dev sh -c "pip install -q -e '.[dev]' && pytest --disable-pytest-warnings -vv -p no:cacheprovider --show-capture=no"

check: lint type format-check test

run:
	python -m taskboy.main
