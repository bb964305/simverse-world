"""Eight-dimension hard budget ledger (PRD §Hard Budgets, V10). ``LabRunBudget``
is storage-only (T1); this module is the counter engine on top of it — atomic
reservation, settlement, direct debit, and exhaustion.

Reservation must be race-safe: two concurrent callers reserving the last unit
of the same dimension must not both succeed. That is a single conditional
``UPDATE ... WHERE (used + reserved + amount) <= limit`` (limit 0 = unlimited,
handled by the predicate, never in Python) plus a rowcount check — mirroring
the Broker's own conditional-UPDATE + rowcount discipline (``app.lab.broker``).
``confirm``/``release`` use the same atomic-UPDATE-in-SQL discipline as
``reserve``/``spend`` (not a Python read-modify-write): two callers settling
the same run+dimension from their own independently-loaded, possibly-stale
row would otherwise silently clobber each other's delta instead of both
landing.

The UPDATE is plain Core SQL, bypassing ORM session sync entirely, so an
already-identity-mapped copy of the row (e.g. the one ``init_run_budget``
handed back earlier in the same session) does not see the change and would
report stale values on its next attribute access. On success, ``_touch``
re-``db.get()``s that specific row and calls ``db.expire(row)`` on it alone —
deliberately *not* ``db.expire_all()``/``db.rollback()``, which expire every
object in the session and were tried first, but broke the Broker: expiring
its already-committed ``LabToolAction`` mid-``execute_action`` made the
caller's very next, un-awaited ``action.result_json`` access crash
(``MissingGreenlet``, since attribute access can't trigger a sync lazy-load
outside a greenlet context). A single-object ``expire()`` leaves every
*other* identity-mapped object — the Broker's action included — untouched;
only the next read of the budget row itself pays for a fresh reload.

Exhaustion is terminal for the run: this module only marks
``exhausted_dimension`` and raises; revoking grants and tearing down the run
is the caller's job (Broker for T5, orchestrator later). Note that each
function below commits its own change as soon as it succeeds — there is no
larger transaction spanning "reserve, then do the work, then confirm/release".
If a caller's own subsequent step fails after a successful ``reserve``/
``spend`` but before it calls ``confirm``/``release``, the reservation is
orphaned (stuck in ``reserved_*`` forever). Reconciling that is out of scope
here — T7's orchestrator owns run-level failure handling.

A run with no ``LabRunBudget`` row (legacy path / never initialised) bypasses
budgeting entirely — every function below is a silent no-op in that case, so
callers that don't opt into budgets see no behaviour change.
"""
from __future__ import annotations

from sqlalchemy import case, or_, select, update
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


async def _touch(db, run_id: str) -> None:
    """Force any already-identity-mapped copy of this run's budget row to
    reload on its next access, without expiring anything else the caller may
    still be holding (see module docstring)."""
    row = await db.get(LabRunBudget, run_id)
    if row is not None:
        db.expire(row)


async def _exhaust(db, row: LabRunBudget, dimension: str) -> None:
    row.exhausted_dimension = dimension
    await db.commit()
    from app.lab import telemetry
    telemetry.emit_alert(
        telemetry.LabAlert.BUDGET_EXHAUSTED,
        run_id=row.run_id, tenant_id=row.tenant_id, dimension=dimension, reason="limit",
    )
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
        await _touch(db, run_id)
        return

    # No row matched — either the dimension is exhausted or the run has no
    # budget row at all (legacy bypass). The UPDATE changed nothing, so
    # there is nothing to roll back; just check which case this is.
    row = await db.get(LabRunBudget, run_id)
    if row is None:
        return  # legacy bypass — no budget tracked for this run
    await _exhaust(db, row, dimension)


async def confirm(db, *, run_id: str, dimension: str, reserved: int = 1, actual: int | None = None) -> None:
    """Settle a reservation as real spend: ``used += actual`` (default =
    ``reserved``), ``reserved -= reserved`` (never below zero). Atomic, like
    ``reserve``/``spend`` — two callers settling the same run+dimension from
    their own independently-loaded rows must not clobber each other's delta
    (each delta is computed in the UPDATE itself, off the row's current
    committed value, not off a Python-side read taken earlier)."""
    _validate_dimension(dimension)
    actual = reserved if actual is None else actual
    table = LabRunBudget.__table__
    used_col, reserved_col = table.c[f"used_{dimension}"], table.c[f"reserved_{dimension}"]

    new_reserved = case((reserved_col - reserved < 0, 0), else_=reserved_col - reserved)
    stmt = (
        update(table)
        .where(table.c.run_id == run_id)
        .values(**{f"used_{dimension}": used_col + actual, f"reserved_{dimension}": new_reserved})
    )
    result = await db.execute(stmt)
    if result.rowcount:
        await db.commit()
        await _touch(db, run_id)
    # rowcount == 0: no budget row for this run (legacy bypass). Nothing was
    # changed, so there is nothing to roll back — just return.


async def release(db, *, run_id: str, dimension: str, amount: int = 1) -> None:
    """Refund a reservation after a deterministic failure: ``reserved -=
    amount`` (never below zero). ``used`` is untouched. Atomic for the same
    lost-update reason as ``confirm``."""
    _validate_dimension(dimension)
    table = LabRunBudget.__table__
    reserved_col = table.c[f"reserved_{dimension}"]

    new_reserved = case((reserved_col - amount < 0, 0), else_=reserved_col - amount)
    stmt = update(table).where(table.c.run_id == run_id).values(**{f"reserved_{dimension}": new_reserved})
    result = await db.execute(stmt)
    if result.rowcount:
        await db.commit()
        await _touch(db, run_id)
    # rowcount == 0: no budget row for this run (legacy bypass). Nothing was
    # changed, so there is nothing to roll back — just return.


async def stage_action_reservation(
    db, *, run_id: str, is_egress: bool
) -> str | None:
    """Stage one action's request-side reservations as a single transaction.

    The caller commits this together with the action (and approval, when one is
    required). Returning a dimension means neither reservation was taken; the
    caller persists the denied action and the exhaustion marker atomically.
    """
    row = await db.scalar(
        select(LabRunBudget)
        .where(LabRunBudget.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None

    dimensions = ["tool_calls"]
    if is_egress:
        dimensions.append("egress_requests")
    for dimension in dimensions:
        limit = getattr(row, f"limit_{dimension}")
        used = getattr(row, f"used_{dimension}")
        reserved = getattr(row, f"reserved_{dimension}")
        if limit and used + reserved + 1 > limit:
            row.exhausted_dimension = dimension
            await db.flush()
            return dimension

    row.reserved_tool_calls += 1
    if is_egress:
        row.reserved_egress_requests += 1
    await db.flush()
    return None


async def stage_action_settlement(
    db,
    *,
    run_id: str,
    succeeded: bool,
    is_egress: bool,
    egress_bytes: int = 0,
) -> str | None:
    """Stage one tool action's budget settlement without committing.

    The Broker composes this with the action's terminal status so a crash can
    never leave a reusable terminal result whose reservations or egress bytes
    were not accounted. The locked budget row serializes concurrent actions.
    Returns the exhausted dimension, if the completed effect exceeded the byte
    limit; the caller commits the truth before raising ``BudgetExhausted``.
    """
    if type(egress_bytes) is not int or egress_bytes < 0:
        raise BudgetError("egress_bytes settlement must be a non-negative integer")
    row = await db.scalar(
        select(LabRunBudget)
        .where(LabRunBudget.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None

    row.reserved_tool_calls = max(0, row.reserved_tool_calls - 1)
    if not succeeded:
        if is_egress:
            row.reserved_egress_requests = max(
                0, row.reserved_egress_requests - 1
            )
        await db.flush()
        return None

    row.used_tool_calls += 1
    if is_egress:
        row.reserved_egress_requests = max(
            0, row.reserved_egress_requests - 1
        )
        row.used_egress_requests += 1
        if (
            row.limit_egress_bytes
            and row.used_egress_bytes
            + row.reserved_egress_bytes
            + egress_bytes
            > row.limit_egress_bytes
        ):
            row.exhausted_dimension = "egress_bytes"
            await db.flush()
            return "egress_bytes"
        row.used_egress_bytes += egress_bytes
    await db.flush()
    return None


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
        await _touch(db, run_id)
        return

    # No row matched — either the dimension is exhausted or the run has no
    # budget row at all (legacy bypass). The UPDATE changed nothing, so
    # there is nothing to roll back; just check which case this is.
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
