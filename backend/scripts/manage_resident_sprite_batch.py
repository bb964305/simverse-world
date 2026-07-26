#!/usr/bin/env python3
"""Prepare, execute, inspect, install, and recover the static 25-slot batch."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.resident_sprite_artifacts import load_run
from app.services.resident_sprite_batch import (
    ResidentSpriteBatchError,
    consolidate_batch,
    install_batch,
    load_batch,
    prepare_batch,
    recover_install,
    reserve_run,
    sync_batch,
)
from app.services.resident_sprite_generation import canonical_json_bytes, validate_run_id


EXIT_VALIDATION = 2
EXIT_GENERATION = 3
EXIT_INSTALL = 4

DEFAULT_CATALOG = REPO_ROOT / "frontend/config/resident-sprite-generation.json"
DEFAULT_AGENTS = REPO_ROOT / "frontend/public/assets/village/agents"
DEFAULT_DENYLIST = REPO_ROOT / "frontend/config/resident-sprite-legacy-denylist.json"
DEFAULT_BATCH_ROOT = BACKEND_ROOT / "var/resident-sprite-batches"
GENERATOR = BACKEND_ROOT / "scripts/generate_resident_sprite.py"
NO_GENERATION_STATES = frozenset({
    "auto_qc_passed",
    "candidate_ready",
    "phaser_reviewed",
    "human_approved",
    "publishing",
    "published",
    "quarantined",
    "rolled_back",
})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the canonical 25-slot sprite replacement batch")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    prepare.add_argument("--agents-root", type=Path, default=DEFAULT_AGENTS)
    prepare.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    prepare.add_argument("--batch-id")
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--price-per-request-usd", required=True)
    prepare.add_argument("--max-cost-usd", required=True)
    prepare.add_argument("--cost-source", required=True)

    for name in ("status", "generate", "install"):
        command = commands.add_parser(name)
        command.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
        command.add_argument("--batch-id", required=True)
        command.add_argument("--artifact-root", type=Path, required=True)
        if name == "generate":
            command.add_argument("--asset-key")
            command.add_argument("--confirm-batch-id", required=True)
            command.add_argument("--confirm-max-requests", type=int, required=True)
            command.add_argument("--confirm-max-cost-usd", required=True)
        if name == "install":
            command.add_argument("--agents-root", type=Path, default=DEFAULT_AGENTS)
            command.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST)
            command.add_argument("--reviewer", action="append", required=True)

    consolidate = commands.add_parser("consolidate")
    consolidate.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    consolidate.add_argument("--batch-id", required=True)
    consolidate.add_argument("--artifact-root", type=Path, required=True)
    consolidate.add_argument("--source-batch-id", required=True)
    consolidate.add_argument("--source-artifact-root", type=Path, required=True)

    recover = commands.add_parser("recover-install")
    recover.add_argument("--agents-root", type=Path, default=DEFAULT_AGENTS)
    recover.add_argument("--action", choices=("finish", "rollback"), required=True)
    return parser


def _summary(batch: Any) -> dict[str, Any]:
    counts: dict[str, int] = {}
    submitted = 0
    for item in batch.items:
        counts[item.run_state] = counts.get(item.run_state, 0) + 1
        submitted += item.submitted_request_count
    return {
        "batch_id": batch.batch_id,
        "catalog_id": batch.catalog_id,
        "model": batch.model,
        "item_count": len(batch.items),
        "states": counts,
        "submitted_request_count": submitted,
        "submitted_cost_upper_bound_usd": str(
            Decimal(batch.price_snapshot.price_per_request_usd) * submitted
        ),
        "max_requests_total": batch.max_requests_total,
        "price_snapshot": batch.price_snapshot.model_dump(mode="json"),
        "worst_case_cost_usd": str(
            Decimal(batch.price_snapshot.price_per_request_usd) * batch.max_requests_total
        ),
        "items": [item.model_dump(mode="json") for item in batch.items],
    }


def _confirm_paid(args: argparse.Namespace, batch: Any) -> None:
    try:
        confirmed_cost = Decimal(args.confirm_max_cost_usd)
        frozen_cost = Decimal(batch.price_snapshot.max_cost_usd)
    except InvalidOperation as exc:
        raise ResidentSpriteBatchError("PAID_CONFIRMATION_INVALID", "confirmed max cost is invalid") from exc
    if (
        args.confirm_batch_id != batch.batch_id
        or args.confirm_max_requests != batch.max_requests_total
        or confirmed_cost != frozen_cost
    ):
        raise ResidentSpriteBatchError(
            "PAID_CONFIRMATION_MISMATCH",
            "paid confirmation must exactly match batch ID, request ceiling, and frozen max cost",
        )


def _generator_env(artifact_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["RESIDENT_SPRITE_ARTIFACT_DIR"] = str(artifact_root)
    return environment


def _invoke_generator(arguments: list[str], artifact_root: Path) -> dict[str, Any]:
    process = subprocess.run(
        [sys.executable, str(GENERATOR), *arguments],
        cwd=BACKEND_ROOT,
        env=_generator_env(artifact_root),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        try:
            error = json.loads(process.stderr).get("error", {})
            code = str(error.get("code", "GENERATION_FAILED"))
            message = str(error.get("message", "resident sprite generation failed"))
        except Exception:
            code, message = "GENERATION_FAILED", "resident sprite generation failed"
        raise ResidentSpriteBatchError(code, message)
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ResidentSpriteBatchError("GENERATOR_OUTPUT_INVALID", "generator output is invalid") from exc


def _item_request_ceiling(item: Any) -> int:
    return 14 if item.direction_policy == "generate_right" else 11


def _generate(args: argparse.Namespace) -> dict[str, Any]:
    batch = sync_batch(args.batch_root, args.batch_id, args.artifact_root)
    _confirm_paid(args, batch)
    selected = [item for item in batch.items if args.asset_key is None or item.asset_key == args.asset_key]
    if not selected:
        raise ResidentSpriteBatchError("BATCH_ITEM_UNKNOWN", "asset key is not part of the batch")
    results = []
    for original in selected:
        batch = sync_batch(args.batch_root, args.batch_id, args.artifact_root)
        current = next(item for item in batch.items if item.asset_key == original.asset_key)
        _, current = reserve_run(args.batch_root, args.batch_id, current.asset_key)
        spec = args.batch_root / args.batch_id / current.request_file
        try:
            run = load_run(args.artifact_root, current.run_id or "")
        except Exception:
            run = None
        if run is not None and run.state in NO_GENERATION_STATES:
            result = {"run_id": run.run_id, "state": run.state, "action": "no_generation"}
        else:
            submitted = sum(item.submitted_request_count for item in batch.items)
            remaining_item = _item_request_ceiling(current) - current.submitted_request_count
            remaining_batch = batch.max_requests_total - submitted
            if remaining_item <= 0 or remaining_batch <= 0:
                raise ResidentSpriteBatchError(
                    "BATCH_BUDGET_EXHAUSTED", "batch request budget is exhausted"
                )

        if run is None:
            result = _invoke_generator(
                ["generate", "--spec", str(spec), "--run-id", current.run_id or ""],
                args.artifact_root,
            )
        elif run.state in {"failed", "interrupted"}:
            result = _invoke_generator(["resume", "--run-id", run.run_id], args.artifact_root)
        elif run.state not in NO_GENERATION_STATES:
            result = _invoke_generator(["resume", "--run-id", run.run_id], args.artifact_root)
        results.append({"asset_key": current.asset_key, **result})
        sync_batch(args.batch_root, args.batch_id, args.artifact_root)
    return {"batch": _summary(load_batch(args.batch_root, args.batch_id)), "results": results}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "prepare":
        batch = prepare_batch(
            catalog_path=args.catalog,
            agents_root=args.agents_root,
            batch_root=args.batch_root,
            model=args.model,
            price_per_request_usd=args.price_per_request_usd,
            max_cost_usd=args.max_cost_usd,
            cost_source=args.cost_source,
            batch_id=args.batch_id,
        )
        return _summary(batch)
    if args.command == "status":
        return _summary(sync_batch(args.batch_root, args.batch_id, args.artifact_root))
    if args.command == "generate":
        return _generate(args)
    if args.command == "consolidate":
        return _summary(
            consolidate_batch(
                batch_root=args.batch_root,
                batch_id=args.batch_id,
                artifact_root=args.artifact_root,
                source_batch_id=args.source_batch_id,
                source_artifact_root=args.source_artifact_root,
            )
        )
    if args.command == "install":
        return install_batch(
            batch_root=args.batch_root,
            batch_id=args.batch_id,
            artifact_root=args.artifact_root,
            agents_root=args.agents_root,
            approved_reviewers=frozenset(args.reviewer),
            denylist_path=args.denylist,
        )
    if args.command == "recover-install":
        return recover_install(args.agents_root, action=args.action)
    raise ResidentSpriteBatchError("COMMAND_INVALID", "unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if hasattr(args, "batch_id") and args.batch_id is not None:
            validate_run_id(args.batch_id)
        result = run(args)
        sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
        return 0
    except ResidentSpriteBatchError as exc:
        sys.stderr.buffer.write(
            canonical_json_bytes({"error": {"code": exc.code, "message": str(exc)}}) + b"\n"
        )
        return EXIT_INSTALL if args.command in {"install", "recover-install"} else EXIT_GENERATION if args.command == "generate" else EXIT_VALIDATION
    except (OSError, ValueError, TypeError):
        sys.stderr.buffer.write(
            canonical_json_bytes({"error": {"code": "VALIDATION_FAILED", "message": "input failed validation"}}) + b"\n"
        )
        return EXIT_VALIDATION


if __name__ == "__main__":
    raise SystemExit(main())
