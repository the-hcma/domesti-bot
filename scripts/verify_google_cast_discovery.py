#!/usr/bin/env python3
"""List Google Cast devices on the LAN (PyChromecast), optional timeout.

Usage (from repo root)::

    uv run python scripts/verify_google_cast_discovery.py
    uv run python scripts/verify_google_cast_discovery.py 8
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.androidtv_device_manager import discover_cast_adb_specs_via_zeroconf


async def _main(timeout: float) -> int:
    uuids, labels, rows = await discover_cast_adb_specs_via_zeroconf(timeout=timeout)
    if not uuids:
        print("No Cast devices discovered.", file=sys.stderr)
        return 1
    for uid in uuids:
        lbl = labels.get(uid, "")
        extra = f"  ({lbl})" if lbl else ""
        print(f"{uid}{extra}")
    print(f"\n{len(rows)} row(s) for SQLite cache (host, port, name).")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Browse _googlecast._tcp via PyChromecast.")
    p.add_argument(
        "timeout",
        nargs="?",
        type=float,
        default=12.0,
        help="Discovery timeout in seconds (default: 12)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_main(float(args.timeout))))
