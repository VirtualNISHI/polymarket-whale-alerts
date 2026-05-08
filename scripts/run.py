"""Cron entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.job import main


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket whale alerts runner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Discord send and DB writes; print embeds to log",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(dry_run=args.dry_run)
