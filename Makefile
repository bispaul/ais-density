.PHONY: lint download ingest all

all: download ingest

lint:
	uv run ruff check .
	uv run mypy src/

download:
	uv run python -m src.download $(ARGS)

ingest:
	uv run python -m src.ingest $(ARGS)
