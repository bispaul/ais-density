.PHONY: lint download ingest grid classify all

all: download ingest grid classify

lint:
	uv run ruff check .
	uv run mypy src/

download:
	uv run python -m src.download $(ARGS)

ingest:
	uv run python -m src.ingest $(ARGS)

grid:
	uv run python -m src.grid $(ARGS)

classify:
	uv run python -m src.classify $(ARGS)
