.PHONY: lint download ingest grid classify visualize all clean help

.DEFAULT_GOAL := help

# Every stage is config-driven: its module loops over all regions and windows in
# config.yaml, so `make all` reproduces the full pipeline from an empty data/
# (raw downloads are reused when cached and hash-verified).
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

# Remove all derived data (keeps raw downloads and vendored static assets).
clean:
	rm -rf data/interim data/processed maps
	rm -f data/run_ledger.csv data/class_ledger.csv

help:
	@echo "Targets: download ingest grid classify visualize all clean lint"
	@echo "  make all              run the full pipeline (config-driven, all regions/windows)"
	@echo "  make <stage> ARGS=--force   re-run one stage, forcing recompute"
	@echo "  make clean            drop derived data (interim/processed/maps/ledgers), keep raw"
