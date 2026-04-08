.PHONY: install test lint clean

install:
	poetry install

test:
	poetry run pytest tests/ -v

lint:
	poetry run ruff check topdown/ tests/

clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
