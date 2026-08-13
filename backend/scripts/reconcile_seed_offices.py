#!/usr/bin/env python3
"""Dry-run/apply reconciliation for the two seed-backed labour offices.

Examples::

    python scripts/reconcile_seed_offices.py
    python scripts/reconcile_seed_offices.py --apply

The default performs no writes. Conflicts, missing duty holders, and ambiguous
holders produce a non-zero exit code in either mode.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import async_session  # noqa: E402
from app.services.office_service import reconcile_seed_offices  # noqa: E402


async def _run(*, apply: bool) -> int:
    async with async_session() as db:
        report = await reconcile_seed_offices(db, apply=apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    unsafe = bool(
        report["missing"] or report["ambiguous"] or report["conflicts"]
    )
    return 2 if unsafe else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="reconcile town_clerk/postman seed duties with offices",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="apply safe vacant appointments (default: read-only dry run)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
