import argparse
import hashlib
from datetime import date
from pathlib import Path

import pooch
from loguru import logger

from src.config import Config, load_config
from src.ingest import RAW_DIR, _days

BASE_URL = "https://noaaocm.blob.core.windows.net/ais/csv2"
REGISTRY_PATH = RAW_DIR / "registry.txt"

_CHUNK = 1 << 20

# We log downloads ourselves and record hashes in the registry, so silence
# pooch's per-file URL and "use this value as known_hash" notices.
pooch.get_logger().setLevel("WARNING")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, str]:
    """Map of downloaded filename → sha256 hex, our local integrity record."""
    if not path.exists():
        return {}
    registry: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, hexdigest = line.split()
        registry[name] = hexdigest
    return registry


def save_registry(registry: dict[str, str], path: Path = REGISTRY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{name} {registry[name]}\n" for name in sorted(registry))
    path.write_text(body)


def _url_for(day: date) -> str:
    return f"{BASE_URL}/csv{day:%Y}/ais-{day:%Y-%m-%d}.csv.zst"


def download_day(
    day: date,
    registry: dict[str, str],
    *,
    force: bool = False,
    raw_dir: Path = RAW_DIR,
) -> Path | None:
    """Fetch one day's raw AIS file, verifying against the local hash registry."""
    fname = f"ais-{day:%Y-%m-%d}.csv.zst"
    dest = raw_dir / fname
    known = registry.get(fname)

    if dest.exists() and not force:
        if known is None:
            registry[fname] = _sha256(dest)
            logger.info("present {} — recorded hash", fname)
            return dest
        if _sha256(dest) == known:
            logger.info("skip {} — already downloaded, hash ok", fname)
            return dest
        logger.warning("hash mismatch for {} — re-downloading", fname)

    if force and dest.exists():
        dest.unlink()

    logger.info("downloading {}", fname)
    fetched = pooch.retrieve(
        url=_url_for(day),
        known_hash=f"sha256:{known}" if known and not force else None,
        fname=fname,
        path=raw_dir,
        progressbar=True,
    )
    registry[fname] = _sha256(Path(fetched))
    return Path(fetched)


def download_all(config: Config, *, force: bool = False) -> list[Path]:
    registry = load_registry()
    days = sorted({d for w in config.windows.values() for d in _days(w.start, w.end)})
    fetched: list[Path] = []
    for day in days:
        path = download_day(day, registry, force=force)
        if path is not None:
            fetched.append(path)
        save_registry(registry)
    return fetched


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download raw AIS daily files (NOAA csv2) for the configured windows."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if the file exists and its hash matches",
    )
    args = parser.parse_args()
    download_all(load_config(args.config), force=args.force)


if __name__ == "__main__":
    main()
