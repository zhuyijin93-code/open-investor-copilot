from __future__ import annotations

import argparse
import csv
from pathlib import Path

from . import data_sources

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT_ROOT = PROJECT_ROOT / "universe_snapshots"
SNAPSHOT_FILES = {
    "a": SNAPSHOT_ROOT / "a_share_snapshot.csv",
    "hk": SNAPSHOT_ROOT / "hk_share_snapshot.csv",
    "us": SNAPSHOT_ROOT / "us_share_snapshot.csv",
    "all": SNAPSHOT_ROOT / "global_snapshot.csv",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build bundled stock-universe snapshots for deployment fallback.")
    parser.add_argument(
        "--market",
        choices=("a", "hk", "us", "all"),
        default="all",
        help="Which snapshot set to build. Default: all.",
    )
    parser.add_argument(
        "--refresh-components",
        action="store_true",
        help="When building all, refresh a/hk/us component snapshots before merging.",
    )
    return parser.parse_args()


def write_global_snapshot() -> Path:
    output = SNAPSHOT_FILES["all"]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for market in ("a", "hk", "us"):
        source = SNAPSHOT_FILES[market]
        with source.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("ticker") or "").strip().upper()
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)
                rows.append(row)
    return data_sources._write_universe_csv(output, rows)


def build_market_snapshot(market: str) -> Path:
    output = SNAPSHOT_FILES[market]
    if market == "a":
        return data_sources.refresh_a_share_universe(output, limit=0, min_amount_yuan=0, max_age_seconds=0)
    if market == "hk":
        return data_sources.refresh_hk_share_universe(output, limit=0, min_amount_hkd=0, max_age_seconds=0)
    if market == "us":
        return data_sources.refresh_us_share_universe(output, limit=0, min_amount_usd=0, max_age_seconds=0)
    raise RuntimeError(f"Unsupported market: {market}")


def main() -> None:
    args = parse_args()
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    if args.market == "all":
        for market in ("a", "hk", "us"):
            source = SNAPSHOT_FILES[market]
            if args.refresh_components or not source.exists():
                path = build_market_snapshot(market)
            else:
                path = source
            print(f"{market}: {path}")
        print(f"all: {write_global_snapshot()}")
        return
    if args.market in {"a", "hk", "us"}:
        print(build_market_snapshot(args.market))
        return
    raise RuntimeError(f"Unsupported market selection: {args.market}")


if __name__ == "__main__":
    main()
