from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from app.lab.model_gateway.config import GatewayConfig


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

    def as_dict(self) -> dict:
        value = asdict(self)
        value["cost_usd_cents"] = (
            self.cost_usd_micros + 9_999
        ) // 10_000
        return value


class UsageLedger:
    def __init__(self, path: str, config: GatewayConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
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
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._db.commit()

    def _empty(self, run_id: str, model: str) -> UsageTotals:
        return UsageTotals(run_id, model, 0, 0, 0, 0, 0, 0)

    def get(self, run_id: str, model: str) -> UsageTotals:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id, model, input_tokens, output_tokens, reasoning_tokens, "
                "total_tokens, cost_usd_micros, request_count FROM run_usage WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return self._empty(run_id, model)
        if row[1] != model:
            raise ValueError("run model changed after usage was recorded")
        return UsageTotals(*row)

    def record(
        self,
        *,
        run_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> UsageTotals:
        if any(type(value) is not int or value < 0 for value in (
            input_tokens, output_tokens, reasoning_tokens
        )):
            raise ValueError("usage values must be non-negative integers")
        input_rate, output_rate = self.config.prices_for(model)
        cost_cny = (
            Decimal(input_tokens) * input_rate
            + Decimal(output_tokens) * output_rate
        ) / Decimal(1_000_000)
        cost_usd_micros = int(
            (cost_cny / self.config.cny_per_usd * Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        total_tokens = input_tokens + output_tokens
        now = int(time.time())
        with self._lock:
            self._db.execute(
                """
                INSERT INTO run_usage (
                    run_id, model, input_tokens, output_tokens, reasoning_tokens,
                    total_tokens, cost_usd_micros, request_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                    total_tokens = total_tokens + excluded.total_tokens,
                    cost_usd_micros = cost_usd_micros + excluded.cost_usd_micros,
                    request_count = request_count + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id, model, input_tokens, output_tokens, reasoning_tokens,
                    total_tokens, cost_usd_micros, now,
                ),
            )
            self._db.commit()
        return self.get(run_id, model)

    def close(self) -> None:
        with self._lock:
            self._db.close()
