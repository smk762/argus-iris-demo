.PHONY: install tape check-tape build serve test lint format
install:  ## venv, and the browser the tests use
	uv sync
	uv run playwright install chromium-headless-shell
tape:  ## Record every scene again
	uv run python -m record
check-tape:
	uv run python -m record --check
build:  ## Static site in out/
	uv run python -m scripts.build
serve: build  ## http://127.0.0.1:8200 with the headers it ships
	uv run python -c "from tests.serve import start; import threading; start(8200); print('http://127.0.0.1:8200'); threading.Event().wait()"
test:
	uv run pytest -q
lint:
	uv run ruff check . && uv run ruff format --check .
format:
	uv run ruff format . && uv run ruff check --fix .
