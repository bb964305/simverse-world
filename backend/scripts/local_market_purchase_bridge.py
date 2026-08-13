#!/usr/bin/env python3
"""Local-only bridge from the visual market loop to the real purchase service.

The ordinary ``local_market_demo.py`` intentionally projects animation only.
This companion is an explicit, write-enabled *temporary demo* adapter: it
listens for that driver's local visit ids, prepares matching rows in one SQLite
database under the OS temporary directory, and publishes only the
``market_purchase`` dictionaries returned by the production service.

Safety boundaries are deliberately narrow:

* ``--run`` is mandatory;
* Redis must be credential-free and loopback with an explicit port;
* the SQLite database must live below the resolved system temp directory;
* only visit ids beginning ``local-market-demo-`` are handled;
* exactly four named autonomous residents are allowed;
* resident positions, balances and import catalog rows are restored on normal
  exit, and all bridge-created event/visit audit rows are removed.

This script never connects to vm212 and never fabricates a purchase frame.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from typing import Any, Sequence
import uuid


WS_CHANNEL = "sv:ws"
LOCAL_VISIT_PREFIX = "local-market-demo-"
LOCAL_EVENT_PREFIX = "local-market-demo-event-"
VISITOR_SLOTS: tuple[tuple[int, int], ...] = (
    (114, 93), (116, 93), (114, 95), (116, 95),
)
DEMO_BALANCE_SC = 10_000


def require_temp_sqlite(path: Path) -> Path:
    """Resolve an existing SQLite file and reject every non-temporary path."""
    resolved = path.expanduser().resolve(strict=True)
    temp_roots = {
        Path(tempfile.gettempdir()).resolve(strict=True),
        # macOS resolves /tmp to /private/tmp even when TMPDIR points at the
        # per-user /var/folders tree. Both are OS-owned disposable roots.
        Path("/tmp").resolve(strict=True),
    }
    if not resolved.is_file():
        raise ValueError(f"database is not a file: {resolved}")
    if not any(root in resolved.parents for root in temp_roots):
        raise ValueError("authoritative demo database must be below the OS temp directory")
    return resolved


def sqlite_async_url(path: Path) -> str:
    return "sqlite+aiosqlite:///" + path.as_posix()


def decode_local_frame(raw: Any) -> dict[str, Any] | None:
    """Return one local demo broadcast payload from a Redis pub/sub message."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str):
        return None
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get("op") != "broadcast":
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    return data


@dataclass(frozen=True)
class ResidentSnapshot:
    resident_id: str
    slug: str
    tile_x: int
    tile_y: int
    status: str


@dataclass(frozen=True)
class ItemSnapshot:
    values: dict[str, Any] | None


class AuthoritativeMarketBridge:
    def __init__(self, slugs: Sequence[str]):
        self.slugs = tuple(slugs)
        self.residents: dict[str, ResidentSnapshot] = {}
        self.treasury_before: dict[str, int | None] = {}
        self.items_before: dict[str, ItemSnapshot] = {}
        self.created_visit_ids: set[str] = set()
        self.created_event_ids: set[str] = set()
        self.current_visit_id: str | None = None
        self.current_event_id: str | None = None
        self.slot_by_slug: dict[str, tuple[int, int]] = {}
        self._purchase_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        from sqlalchemy import select
        from app.database import async_session, engine
        from app.models.caravan_visit import CaravanMarketVisitor
        from app.models.resident import Resident
        from app.models.resident_treasury import ResidentTreasury
        from app.models.shop import Item
        from app.services.caravan_service import IMPORT_DEFS

        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: CaravanMarketVisitor.__table__.create(sync, checkfirst=True)
            )

        async with async_session() as db:
            rows = (await db.execute(
                select(Resident).where(Resident.slug.in_(self.slugs))
            )).scalars().all()
            by_slug = {row.slug: row for row in rows}
            missing = sorted(set(self.slugs) - set(by_slug))
            if missing:
                raise ValueError("demo residents missing from SQLite: " + ", ".join(missing))
            for slug in self.slugs:
                resident = by_slug[slug]
                if not resident.is_autonomous:
                    raise ValueError(f"demo resident is not autonomous: {slug}")
                self.residents[slug] = ResidentSnapshot(
                    resident_id=str(resident.id), slug=slug,
                    tile_x=int(resident.tile_x), tile_y=int(resident.tile_y),
                    status=str(resident.status),
                )
                treasury = await db.get(ResidentTreasury, slug)
                self.treasury_before[slug] = (
                    None if treasury is None else int(treasury.balance_sc)
                )
                if treasury is None:
                    db.add(ResidentTreasury(
                        resident_slug=slug, balance_sc=DEMO_BALANCE_SC,
                    ))
                else:
                    treasury.balance_sc = DEMO_BALANCE_SC

            for definition in IMPORT_DEFS:
                item = (await db.execute(
                    select(Item).where(Item.code == definition["code"])
                )).scalar_one_or_none()
                if item is None:
                    self.items_before[definition["code"]] = ItemSnapshot(None)
                else:
                    self.items_before[definition["code"]] = ItemSnapshot({
                        "id": item.id, "code": item.code, "kind": item.kind,
                        "name": item.name, "description": item.description,
                        "icon": item.icon, "price_sc": int(item.price_sc),
                        "payload_json": deepcopy(item.payload_json or {}),
                        "stock": item.stock, "active": bool(item.active),
                    })
            await db.commit()

    async def prepare_visit(self, frame: dict[str, Any]) -> None:
        from app.database import async_session
        from app.models.caravan_visit import CaravanVisit
        from app.models.world_event import WorldEvent

        visit_id = str(frame.get("visit_id") or "")
        event_id = str(frame.get("world_event_id") or "")
        if not visit_id.startswith(LOCAL_VISIT_PREFIX):
            return
        if not event_id.startswith(LOCAL_EVENT_PREFIX):
            raise ValueError("local visit carried a non-local event id")
        now = datetime.now(UTC)
        position = frame.get("position") or {}
        async with async_session() as db:
            if await db.get(CaravanVisit, visit_id) is not None:
                raise ValueError(f"local demo visit already exists: {visit_id}")
            db.add(WorldEvent(
                id=event_id, type="festival", title="本地权威集市演示",
                description="四名自治居民在集市大厅购买商队进口货。",
                payload_json={"market_day": True, "location_id": "market_hall"},
                starts_at=now, ends_at=now + timedelta(hours=1), is_active=True,
            ))
            db.add(CaravanVisit(
                id=visit_id, world_event_id=event_id, phase="waiting",
                visibility_slot="world", version=max(1, int(frame.get("version") or 1)),
                next_action_at=now + timedelta(hours=1),
                tile_x=int(position.get("tile_x") or 102),
                tile_y=int(position.get("tile_y") or 127),
                summary_json={},
            ))
            await db.commit()
        self.created_visit_ids.add(visit_id)
        self.created_event_ids.add(event_id)
        self.current_visit_id = visit_id
        self.current_event_id = event_id
        self.slot_by_slug.clear()

    async def set_phase(self, phase: str, frame: dict[str, Any]) -> None:
        from app.database import async_session
        from app.models.caravan_visit import CaravanVisit

        visit_id = str(frame.get("visit_id") or "")
        if visit_id != self.current_visit_id:
            return
        async with async_session() as db:
            visit = await db.get(CaravanVisit, visit_id)
            if visit is None:
                return
            visit.phase = phase
            visit.version = max(int(visit.version), int(frame.get("version") or 1))
            position = frame.get("position") or {}
            if isinstance(position, dict):
                visit.tile_x = int(position.get("tile_x") or visit.tile_x)
                visit.tile_y = int(position.get("tile_y") or visit.tile_y)
            if phase in {"outbound", "departed"}:
                visit.visibility_slot = None if phase == "departed" else "world"
            if phase == "departed":
                visit.departed_at = datetime.now(UTC)
            await db.commit()

    async def record_resident_move(self, frame: dict[str, Any]) -> None:
        slug = str(frame.get("resident_slug") or "")
        if self.current_visit_id is None or slug not in self.residents:
            return
        target = frame.get("target_tile")
        if (isinstance(target, list) and len(target) == 2
                and tuple(target) in VISITOR_SLOTS):
            self.slot_by_slug[slug] = (int(target[0]), int(target[1]))
        tile = (int(frame.get("tile_x") or 0), int(frame.get("tile_y") or 0))
        at_slot = tile == self.slot_by_slug.get(slug)
        start = self.residents[slug]
        at_home = tile == (start.tile_x, start.tile_y)
        if not (at_slot or at_home):
            return
        from app.database import async_session
        from app.models.resident import Resident
        from sqlalchemy import select

        async with async_session() as db:
            resident = (await db.execute(
                select(Resident).where(Resident.slug == slug)
            )).scalar_one()
            resident.tile_x, resident.tile_y = tile
            resident.status = "idle"
            await db.commit()

    async def trade_and_publish(self, publish) -> None:
        """Create durable assignments, stock this visit, then buy once each."""
        if self.current_visit_id is None:
            return
        if set(self.slot_by_slug) != set(self.slugs):
            raise ValueError("trading began before all four resident slots were observed")
        from sqlalchemy import select
        from app.database import async_session
        from app.models.caravan_visit import CaravanMarketVisitor, CaravanVisit
        from app.models.resident import Resident
        from app.services.caravan_service import _stock_import_goods_pending
        from app.services.caravan_market_service import maybe_purchase_for_resident

        visit_id = self.current_visit_id
        now = datetime.now(UTC)
        async with async_session() as db:
            visit = await db.get(CaravanVisit, visit_id)
            if visit is None:
                return
            visit.phase = "trading"
            visit.next_action_at = now + timedelta(minutes=30)
            for slug in self.slugs:
                snapshot = self.residents[slug]
                slot = self.slot_by_slug[slug]
                slot_index = VISITOR_SLOTS.index(slot)
                db.add(CaravanMarketVisitor(
                    id=str(uuid.uuid4()), visit_id=visit_id,
                    resident_id=snapshot.resident_id, resident_slug=slug,
                    slot_index=slot_index, created_at=now,
                ))
                resident = (await db.execute(
                    select(Resident).where(Resident.slug == slug)
                )).scalar_one()
                resident.tile_x, resident.tile_y = slot
                resident.status = "idle"
            await _stock_import_goods_pending(db, visit_id)
            visit.imports_stocked_at = now
            visit.settled_at = now
            await db.commit()

        # The final browser tween is 800 ms. Waiting one second keeps the
        # visible purchase causally behind the real arrival while still leaving
        # ample time before the 18-second trading phase closes.
        await asyncio.sleep(1.0)
        for slug in sorted(self.slugs, key=lambda value: VISITOR_SLOTS.index(
            self.slot_by_slug[value]
        )):
            async with async_session() as db:
                resident = (await db.execute(
                    select(Resident).where(Resident.slug == slug)
                )).scalar_one()
                committed = await maybe_purchase_for_resident(db, resident)
            if committed is not None:
                await publish(committed)
                await asyncio.sleep(0.55)

    async def close_visit(self, phase: str, frame: dict[str, Any]) -> None:
        if self.current_visit_id is None:
            return
        from sqlalchemy import select
        from app.database import async_session
        from app.models.caravan_visit import CaravanVisit
        from app.models.resident import Resident
        from app.models.shop import Item

        visit_id = self.current_visit_id
        async with async_session() as db:
            visit = await db.get(CaravanVisit, visit_id)
            if visit is None:
                return
            now = datetime.now(UTC)
            if visit.imports_withdrawn_at is None:
                rows = (await db.execute(
                    select(Item).where(Item.kind == "import_good")
                )).scalars().all()
                for item in rows:
                    if (item.payload_json or {}).get("caravan_visit_id") == visit_id:
                        item.active = False
                visit.imports_withdrawn_at = now
            visit.phase = phase
            visit.visibility_slot = None if phase == "departed" else "world"
            if phase == "departed":
                visit.departed_at = now
                for snapshot in self.residents.values():
                    resident = (await db.execute(
                        select(Resident).where(Resident.slug == snapshot.slug)
                    )).scalar_one()
                    resident.tile_x, resident.tile_y = snapshot.tile_x, snapshot.tile_y
                    resident.status = snapshot.status
            await db.commit()
        if phase == "departed":
            self.current_visit_id = None
            self.current_event_id = None
            self.slot_by_slug.clear()

    async def handle(self, frame: dict[str, Any], publish) -> None:
        frame_type = frame.get("type")
        if frame_type == "resident_move":
            await self.record_resident_move(frame)
            return
        if frame_type != "caravan_state":
            return
        visit_id = str(frame.get("visit_id") or "")
        if not visit_id.startswith(LOCAL_VISIT_PREFIX):
            return
        phase = frame.get("phase")
        if phase == "waiting":
            await self.prepare_visit(frame)
        elif phase == "inbound":
            await self.set_phase("inbound", frame)
        elif phase == "trading":
            await self.set_phase("trading", frame)
            await self.trade_and_publish(publish)
        elif phase == "outbound":
            await self.close_visit("outbound", frame)
        elif phase == "departed":
            await self.close_visit("departed", frame)

    async def restore(self) -> None:
        """Restore every pre-demo mutable value after publishing has stopped."""
        from sqlalchemy import delete, select
        from app.database import async_session
        from app.models.caravan_visit import CaravanMarketVisitor, CaravanVisit
        from app.models.resident import Resident
        from app.models.resident_treasury import ResidentTreasury
        from app.models.shop import Item
        from app.models.world_event import WorldEvent

        async with async_session() as db:
            for snapshot in self.residents.values():
                resident = (await db.execute(
                    select(Resident).where(Resident.slug == snapshot.slug)
                )).scalar_one_or_none()
                if resident is not None:
                    resident.tile_x, resident.tile_y = snapshot.tile_x, snapshot.tile_y
                    resident.status = snapshot.status
            for slug, balance in self.treasury_before.items():
                row = await db.get(ResidentTreasury, slug)
                if balance is None:
                    if row is not None:
                        await db.delete(row)
                elif row is None:
                    db.add(ResidentTreasury(resident_slug=slug, balance_sc=balance))
                else:
                    row.balance_sc = balance
            for code, snapshot in self.items_before.items():
                row = (await db.execute(
                    select(Item).where(Item.code == code)
                )).scalar_one_or_none()
                if snapshot.values is None:
                    if row is not None:
                        await db.delete(row)
                    continue
                values = deepcopy(snapshot.values)
                if row is None:
                    db.add(Item(**values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
            if self.created_visit_ids:
                await db.execute(delete(CaravanMarketVisitor).where(
                    CaravanMarketVisitor.visit_id.in_(self.created_visit_ids)
                ))
                await db.execute(delete(CaravanVisit).where(
                    CaravanVisit.id.in_(self.created_visit_ids)
                ))
            if self.created_event_ids:
                await db.execute(delete(WorldEvent).where(
                    WorldEvent.id.in_(self.created_event_ids)
                ))
            await db.commit()


async def run_bridge(redis_url: str, bridge: AuthoritativeMarketBridge) -> None:
    import redis.asyncio as aioredis
    from scripts.local_market_demo import validate_redis_url

    validate_redis_url(redis_url)
    client = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()

    async def publish(data: dict[str, Any]) -> None:
        envelope = {"op": "broadcast", "data": data, "exclude": None}
        await client.publish(WS_CHANNEL, json.dumps(envelope, ensure_ascii=False))

    await client.ping()
    await pubsub.subscribe(WS_CHANNEL)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    print("status=running; authoritative purchase frames only")
    try:
        while not stop.is_set():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if message is None:
                continue
            frame = decode_local_frame(message.get("data"))
            if frame is not None:
                await bridge.handle(frame, publish)
    finally:
        await pubsub.unsubscribe(WS_CHANNEL)
        await pubsub.aclose()
        await client.aclose()
        await asyncio.shield(bridge.restore())
        print("status=stopped-and-database-restored")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--resident-slug", action="append", default=[])
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    if not args.run:
        raise ValueError("this write-enabled local bridge requires explicit --run")
    if len(args.resident_slug) != 4 or len(set(args.resident_slug)) != 4:
        raise ValueError("provide exactly four distinct --resident-slug values")
    database = require_temp_sqlite(args.db)
    from scripts.local_market_demo import validate_redis_url
    validate_redis_url(args.redis_url)

    # These must be set before the first app import: app.config is instantiated
    # at module import time and the production service owns both gates.
    os.environ["DATABASE_URL"] = sqlite_async_url(database)
    os.environ["REDIS_URL"] = args.redis_url
    os.environ["NPC_ECONOMY_ENABLED"] = "true"
    os.environ["CARAVAN_LIFECYCLE_ENABLED"] = "true"

    bridge = AuthoritativeMarketBridge(args.resident_slug)
    await bridge.initialize()
    print(f"database={database}")
    print("residents=" + ",".join(args.resident_slug))
    await run_bridge(args.redis_url, bridge)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
