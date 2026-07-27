from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from app.lab.model_gateway.config import GatewayConfig


class BudgetReservationError(RuntimeError):
    pass


class RunRevokedError(BudgetReservationError):
    pass


class UsageUnknownError(BudgetReservationError):
    pass


class InflightLimitError(BudgetReservationError):
    pass


@dataclass(frozen=True)
class UsageTotals:
    run_id: str
    model: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd_micros: int
    request_count: int
    reserved_tokens: int
    reserved_cost_usd_micros: int
    inflight_requests: int
    cost_unknown: bool
    revoked: bool

    def as_dict(self) -> dict:
        value = asdict(self)
        value["cost_usd_cents"] = (
            self.cost_usd_micros + 9_999
        ) // 10_000
        value["reserved_cost_usd_cents"] = (
            self.reserved_cost_usd_micros + 9_999
        ) // 10_000
        return value


class UsageLedger:
    def __init__(self, path: str, config: GatewayConfig) -> None:
        self.config = config
        self.instance_id = str(uuid.uuid4())
        self._reservation_lease_s = max(60, int(config.request_timeout_s) + 60)
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS run_usage (
                run_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd_micros INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_cost_usd_micros INTEGER NOT NULL DEFAULT 0,
                inflight_requests INTEGER NOT NULL DEFAULT 0,
                cost_unknown INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._upgrade_run_usage()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_reservations (
                reservation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                model TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL,
                reserved_cost_usd_micros INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                owner_id TEXT NOT NULL,
                lease_expires_at INTEGER NOT NULL,
                FOREIGN KEY(run_id) REFERENCES run_usage(run_id) ON DELETE CASCADE
            )
            """
        )
        self._upgrade_usage_reservations()
        self._recover_inflight()
        self._db.commit()

    def _recover_inflight(self) -> None:
        now = int(time.time())
        expired = list(self._db.execute(
            "SELECT run_id, SUM(reserved_tokens), "
            "SUM(reserved_cost_usd_micros), COUNT(*) "
            "FROM usage_reservations WHERE lease_expires_at <= ? GROUP BY run_id",
            (now,),
        ))
        if not expired:
            return
        for run_id, reserved_tokens, reserved_cost, inflight in expired:
            self._db.execute(
                "UPDATE run_usage SET cost_unknown = 1, revoked = 1, "
                "reserved_tokens = MAX(0, reserved_tokens - ?), "
                "reserved_cost_usd_micros = "
                "MAX(0, reserved_cost_usd_micros - ?), "
                "inflight_requests = MAX(0, inflight_requests - ?), "
                "updated_at = ? WHERE run_id = ?",
                (reserved_tokens, reserved_cost, inflight, now, run_id),
            )
        self._db.execute(
            "DELETE FROM usage_reservations WHERE lease_expires_at <= ?", (now,)
        )

    def _upgrade_run_usage(self) -> None:
        columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(run_usage)")
        }
        additions = {
            "reserved_tokens": "INTEGER NOT NULL DEFAULT 0",
            "reserved_cost_usd_micros": "INTEGER NOT NULL DEFAULT 0",
            "inflight_requests": "INTEGER NOT NULL DEFAULT 0",
            "cost_unknown": "INTEGER NOT NULL DEFAULT 0",
            "revoked": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self._db.execute(
                    f"ALTER TABLE run_usage ADD COLUMN {name} {declaration}"
                )

    def _upgrade_usage_reservations(self) -> None:
        columns = {
            row[1]
            for row in self._db.execute("PRAGMA table_info(usage_reservations)")
        }
        additions = {
            "owner_id": "TEXT NOT NULL DEFAULT ''",
            "lease_expires_at": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self._db.execute(
                    f"ALTER TABLE usage_reservations ADD COLUMN {name} {declaration}"
                )

    def _cost_micros(self, model: str, input_tokens: int, output_tokens: int) -> int:
        input_rate, output_rate = self.config.prices_for(model)
        cost_cny = (
            Decimal(input_tokens) * input_rate
            + Decimal(output_tokens) * output_rate
        ) / Decimal(1_000_000)
        return int(
            (cost_cny / self.config.cny_per_usd * Decimal(1_000_000))
            .to_integral_value(rounding=ROUND_CEILING)
        )

    @staticmethod
    def _empty(run_id: str, model: str) -> UsageTotals:
        return UsageTotals(run_id, model, 0, 0, 0, 0, 0, 0, 0, 0, 0, False, False)

    @staticmethod
    def _totals(row) -> UsageTotals:
        return UsageTotals(
            run_id=row[0], model=row[1], input_tokens=row[2], output_tokens=row[3],
            reasoning_tokens=row[4], total_tokens=row[5], cost_usd_micros=row[6],
            request_count=row[7], reserved_tokens=row[8],
            reserved_cost_usd_micros=row[9], inflight_requests=row[10],
            cost_unknown=bool(row[11]), revoked=bool(row[12]),
        )

    def _select(self, run_id: str):
        return self._db.execute(
            "SELECT run_id, model, input_tokens, output_tokens, reasoning_tokens, "
            "total_tokens, cost_usd_micros, request_count, reserved_tokens, "
            "reserved_cost_usd_micros, inflight_requests, cost_unknown, revoked "
            "FROM run_usage WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    def get(self, run_id: str, model: str) -> UsageTotals:
        with self._lock:
            row = self._select(run_id)
        if row is None:
            return self._empty(run_id, model)
        if row[1] != model:
            raise ValueError("run model changed after usage was recorded")
        return self._totals(row)

    def reserve(
        self,
        *,
        run_id: str,
        model: str,
        estimated_input_tokens: int,
        max_output_tokens: int,
        max_model_tokens: int,
        budget_usd_cents: int,
        max_inflight_requests: int,
    ) -> str:
        values = (
            estimated_input_tokens, max_output_tokens, max_model_tokens,
            budget_usd_cents, max_inflight_requests,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("reservation limits must be positive integers")
        reserved_tokens = estimated_input_tokens + max_output_tokens
        reserved_cost = self._cost_micros(
            model, estimated_input_tokens, max_output_tokens
        )
        reservation_id = str(uuid.uuid4())
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._recover_inflight()
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO run_usage (run_id, model, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(run_id) DO NOTHING",
                    (run_id, model, now),
                )
                row = self._select(run_id)
                if row is None or row[1] != model:
                    raise ValueError("run model changed after usage was recorded")
                totals = self._totals(row)
                if totals.revoked:
                    raise RunRevokedError("run token has been revoked")
                if totals.cost_unknown:
                    raise UsageUnknownError("run model cost is unknown")
                if totals.inflight_requests >= max_inflight_requests:
                    raise InflightLimitError("run has too many in-flight model requests")
                if totals.total_tokens + totals.reserved_tokens + reserved_tokens > max_model_tokens:
                    raise BudgetReservationError("run model-token budget is exhausted")
                if (
                    totals.cost_usd_micros
                    + totals.reserved_cost_usd_micros
                    + reserved_cost
                    > budget_usd_cents * 10_000
                ):
                    raise BudgetReservationError("run model-cost budget is exhausted")
                changed = self._db.execute(
                    """
                    UPDATE run_usage SET
                        reserved_tokens = reserved_tokens + ?,
                        reserved_cost_usd_micros = reserved_cost_usd_micros + ?,
                        inflight_requests = inflight_requests + 1,
                        updated_at = ?
                    WHERE run_id = ? AND revoked = 0 AND cost_unknown = 0
                      AND inflight_requests < ?
                      AND total_tokens + reserved_tokens + ? <= ?
                      AND cost_usd_micros + reserved_cost_usd_micros + ? <= ?
                    """,
                    (
                        reserved_tokens, reserved_cost, now, run_id,
                        max_inflight_requests, reserved_tokens, max_model_tokens,
                        reserved_cost, budget_usd_cents * 10_000,
                    ),
                )
                if changed.rowcount != 1:
                    raise BudgetReservationError("run budget reservation raced")
                self._db.execute(
                    "INSERT INTO usage_reservations "
                    "(reservation_id, run_id, model, reserved_tokens, "
                    "reserved_cost_usd_micros, created_at, owner_id, lease_expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reservation_id, run_id, model, reserved_tokens,
                        reserved_cost, now, self.instance_id,
                        now + self._reservation_lease_s,
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return reservation_id

    def release(self, reservation_id: str) -> None:
        self._finish_reservation(reservation_id, usage=None, cost_unknown=False)

    def settle(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> UsageTotals:
        if any(
            type(value) is not int or value < 0
            for value in (input_tokens, output_tokens, reasoning_tokens)
        ):
            raise ValueError("usage values must be non-negative integers")
        result = self._finish_reservation(
            reservation_id,
            usage=(input_tokens, output_tokens, reasoning_tokens),
            cost_unknown=False,
        )
        assert result is not None
        return result

    def mark_unknown(self, reservation_id: str) -> UsageTotals:
        result = self._finish_reservation(
            reservation_id, usage=None, cost_unknown=True
        )
        assert result is not None
        return result

    def _finish_reservation(
        self,
        reservation_id: str,
        *,
        usage: tuple[int, int, int] | None,
        cost_unknown: bool,
    ) -> UsageTotals | None:
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                reservation = self._db.execute(
                    "SELECT run_id, model, reserved_tokens, reserved_cost_usd_micros "
                    "FROM usage_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if reservation is None:
                    raise ValueError("usage reservation does not exist")
                run_id, model, reserved_tokens, reserved_cost = reservation
                input_tokens = output_tokens = reasoning_tokens = actual_cost = 0
                request_increment = 0
                if usage is not None:
                    input_tokens, output_tokens, reasoning_tokens = usage
                    actual_cost = self._cost_micros(model, input_tokens, output_tokens)
                    request_increment = 1
                self._db.execute(
                    """
                    UPDATE run_usage SET
                        input_tokens = input_tokens + ?,
                        output_tokens = output_tokens + ?,
                        reasoning_tokens = reasoning_tokens + ?,
                        total_tokens = total_tokens + ?,
                        cost_usd_micros = cost_usd_micros + ?,
                        request_count = request_count + ?,
                        reserved_tokens = reserved_tokens - ?,
                        reserved_cost_usd_micros = reserved_cost_usd_micros - ?,
                        inflight_requests = inflight_requests - 1,
                        cost_unknown = CASE WHEN ? THEN 1 ELSE cost_unknown END,
                        revoked = CASE WHEN ? THEN 1 ELSE revoked END,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        input_tokens, output_tokens, reasoning_tokens,
                        input_tokens + output_tokens, actual_cost, request_increment,
                        reserved_tokens, reserved_cost, int(cost_unknown),
                        int(cost_unknown), now, run_id,
                    ),
                )
                self._db.execute(
                    "DELETE FROM usage_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                row = self._select(run_id)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self._totals(row) if row is not None else None

    def revoke(self, run_id: str, model: str) -> UsageTotals:
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO run_usage (run_id, model, revoked, updated_at) "
                    "VALUES (?, ?, 1, ?) ON CONFLICT(run_id) DO UPDATE SET "
                    "revoked = 1, updated_at = excluded.updated_at",
                    (run_id, model, now),
                )
                row = self._select(run_id)
                if row is None or row[1] != model:
                    raise ValueError("run model changed after usage was recorded")
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self._totals(row)

    def close(self) -> None:
        with self._lock:
            reservation_ids = [
                row[0]
                for row in self._db.execute(
                    "SELECT reservation_id FROM usage_reservations WHERE owner_id = ?",
                    (self.instance_id,),
                )
            ]
        for reservation_id in reservation_ids:
            try:
                self.mark_unknown(reservation_id)
            except ValueError:
                pass
        with self._lock:
            self._db.close()
