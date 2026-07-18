"""Eight-dimension hard budget ledger (PRD §Hard Budgets, V10). ``LabRunBudget``
is storage-only (T1); this module is the counter engine on top of it — atomic
reservation, settlement, direct debit, and exhaustion.

Reservation must be race-safe: two concurrent callers reserving the last unit
of the same dimension must not both succeed. That is a single conditional
``UPDATE ... WHERE (used + reserved + amount) <= limit`` (limit 0 = unlimited,
handled by the predicate, never in Python) plus a rowcount check — mirroring
the Broker's own conditional-UPDATE + rowcount + rollback-then-re-read-by-id
discipline (``app.lab.broker``). The UPDATE is plain Core SQL, bypassing ORM
session sync entirely, so on success it calls ``db.expire_all()`` (the same
whole-session expire ``rollback()`` performs by default) rather than letting
SQLAlchemy's ORM-update sync machinery partially expire individual columns —
a *partial* column expiry on an already-identity-mapped row is never
refreshed by a later ``db.get()`` (that only returns the identity-map hit
as-is), so a caller touching the attribute afterward would either read stale
data or, if literally marked expired, crash on a sync lazy-load outside any
greenlet context. A *whole-instance* expire, by contrast, is exactly what
``db.get()`` already knows how to transparently reload — proven by the
Broker's own rollback-then-``db.get()`` re-read. ``confirm``/``release``
settle a reservation already claimed by exactly one caller (the Broker's
atomic action lifecycle), so a plain read-modify-write is enough — no
contending writer to race against.

Exhaustion is terminal for the run: this module only marks
``exhausted_dimension`` and raises; revoking grants and tearing down the run
is the caller's job (Broker for T5, orchestrator later).

A run with no ``LabRunBudget`` row (legacy path / never initialised) bypasses
budgeting entirely — every function below is a silent no-op in that case, so
callers that don't opt into budgets see no behaviour change.
"""
from __future__ import annotations

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.lab_budget import LabRunBudget

DIMENSIONS = (
    "model_tokens", "tool_calls", "wall_clock_ms", "egress_requests",
    "egress_bytes", "artifact_count", "artifact_bytes", "active_workers",
)


class BudgetError(Exception):
    """Bad input to this module (e.g. an unknown dimension name)."""


class BudgetExhausted(Exception):
    """A reservation or direct debit would exceed the run's hard limit for
    ``dimension``. Terminal for the run — the caller must tear it down."""

    def __init__(self, dimension: str):
        super().__init__(f"budget exhausted: {dimension}")
        self.dimension = dimension


def _validate_dimension(dimension: str) -> None:
    if dimension not in DIMENSIONS:
        raise BudgetError(f"unknown budget dimension: {dimension}")


async def _exhaust(db, row: LabRunBudget, dimension: str) -> None:
    row.exhausted_dimension = dimension
    await db.commit()
    raise BudgetExhausted(dimension)


async def init_run_budget(db, *, run_id: str, tenant_id: str, limits: dict | None = None) -> LabRunBudget:
    """Create the run's budget row, seeding limits from ``settings.lab_budget_*``
    (overridable per-dimension via ``limits``). Idempotent — a second call for
    the same ``run_id`` returns the existing row unchanged (resume semantics)."""
    existing = await db.get(LabRunBudget, run_id)
    if existing is not None:
        return existing

    limits = limits or {}
    values = {
        f"limit_{dim}": limits.get(dim, getattr(settings, f"lab_budget_{dim}"))
        for dim in DIMENSIONS
    }
    row = LabRunBudget(run_id=run_id, tenant_id=tenant_id, **values)
    db.add(row)
    try:
        await db.commit()
        return row
    except IntegrityError:
        await db.rollback()
        existing = await db.get(LabRunBudget, run_id)
        if existing is not None:
            return existing
        raise


async def reserve(db, *, run_id: str, dimension: str, amount: int = 1) -> None:
    """Atomically pre-commit ``amount`` units of ``dimension`` before the work
    it pays for actually happens. ``limit == 0`` means unlimited (predicate
    short-circuits, no Python-side branch to race on)."""
    _validate_dimension(dimension)
    table = LabRunBudget.__table__
    limit_col, used_col, reserved_col = (table.c[f"{p}_{dimension}"] for p in ("limit", "used", "reserved"))

    stmt = (
        update(table)
        .where(
            table.c.run_id == run_id,
            or_(limit_col == 0, (used_col + reserved_col + amount) <= limit_col),
        )
        .values(**{f"reserved_{dimension}": reserved_col + amount})
    )
    result = await db.execute(stmt)
    if result.rowcount == 1:
        await db.commit()
        db.expire_all()
        return

    await db.rollback()
    row = await db.get(LabRunBudget, run_id)
    if row is None:
        return  # legacy bypass — no budget tracked for this run
    await _exhaust(db, row, dimension)


async def confirm(db, *, run_id: str, dimension: str, reserved: int = 1, actual: int | None = None) -> None:
    """Settle a reservation as real spend: ``used += actual`` (default =
    ``reserved``), ``reserved -= reserved`` (never below zero)."""
    _validate_dimension(dimension)
    row = await db.get(LabRunBudget, run_id)
    if row is None:
        return  # legacy bypass — no budget tracked for this run
    actual = reserved if actual is None else actual
    setattr(row, f"used_{dimension}", getattr(row, f"used_{dimension}") + actual)
    setattr(row, f"reserved_{dimension}", max(0, getattr(row, f"reserved_{dimension}") - reserved))
    await db.commit()


async def release(db, *, run_id: str, dimension: str, amount: int = 1) -> None:
    """Refund a reservation after a deterministic failure: ``reserved -=
    amount`` (never below zero). ``used`` is untouched."""
    _validate_dimension(dimension)
    row = await db.get(LabRunBudget, run_id)
    if row is None:
        return  # legacy bypass — no budget tracked for this run
    setattr(row, f"reserved_{dimension}", max(0, getattr(row, f"reserved_{dimension}") - amount))
    await db.commit()


async def spend(db, *, run_id: str, dimension: str, amount: int) -> None:
    """Direct debit with no prior reservation (streamed model-token billing):
    ``used += amount``, atomically bounded by the same limit predicate as
    ``reserve``. Over-limit raises without touching the counter."""
    _validate_dimension(dimension)
    table = LabRunBudget.__table__
    limit_col, used_col, reserved_col = (table.c[f"{p}_{dimension}"] for p in ("limit", "used", "reserved"))

    stmt = (
        update(table)
        .where(
            table.c.run_id == run_id,
            or_(limit_col == 0, (used_col + reserved_col + amount) <= limit_col),
        )
        .values(**{f"used_{dimension}": used_col + amount})
    )
    result = await db.execute(stmt)
    if result.rowcount == 1:
        await db.commit()
        db.expire_all()
        return

    await db.rollback()
    row = await db.get(LabRunBudget, run_id)
    if row is None:
        return  # legacy bypass — no budget tracked for this run
    await _exhaust(db, row, dimension)


async def is_exhausted(db, run_id: str) -> str | None:
    row = await db.get(LabRunBudget, run_id)
    return row.exhausted_dimension if row else None


async def snapshot(db, run_id: str) -> dict:
    row = await db.get(LabRunBudget, run_id)
    if row is None:
        return {}
    return {
        dim: {
            "limit": getattr(row, f"limit_{dim}"),
            "used": getattr(row, f"used_{dim}"),
            "reserved": getattr(row, f"reserved_{dim}"),
        }
        for dim in DIMENSIONS
    }
