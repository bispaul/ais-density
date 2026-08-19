.PHONY: lint download ingest grid classify visualize all

all: download ingest grid classify visualize

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

visualize:
	uv run python -m src.visualize $(ARGS)
