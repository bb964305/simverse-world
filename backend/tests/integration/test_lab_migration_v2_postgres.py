"""Real-PostgreSQL migration boundary for immutable Lab protocol versions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.lab_runtime import (
    LabRuntimeIntent,
    LabRuntimeResult,
    LabRuntimeSession,
    LabRuntimeTurn,
)


pytestmark = [pytest.mark.lab_postgres, pytest.mark.anyio]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
TRUE = {"1", "true", "yes", "on"}
RUNTIME_BINDING_MODELS = {
    "lab_runtime_turns": LabRuntimeTurn,
    "lab_runtime_intents": LabRuntimeIntent,
    "lab_runtime_results": LabRuntimeResult,
}
RUNTIME_CONSTRAINT_MODELS = {
    "lab_runtime_sessions": LabRuntimeSession,
    **RUNTIME_BINDING_MODELS,
}


def _orm_constraint_shape(model) -> dict[str, dict[tuple, str | None]]:
    foreign_keys = {}
    uniques = {}
    for constraint in model.__table__.constraints:
        if isinstance(constraint, ForeignKeyConstraint):
            elements = constraint.elements
            binding = (
                tuple(element.parent.name for element in elements),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
            )
            foreign_keys[binding] = constraint.name
        elif isinstance(constraint, UniqueConstraint):
            uniques[tuple(constraint.columns.keys())] = constraint.name
    return {"foreign_keys": foreign_keys, "uniques": uniques}


def _migration_constraint_shapes(sync_connection) -> dict[str, dict]:
    inspector = inspect(sync_connection)
    shapes = {}
    for table_name in RUNTIME_CONSTRAINT_MODELS:
        foreign_keys = {
            (
                tuple(constraint["constrained_columns"]),
                constraint["referred_table"],
                tuple(constraint["referred_columns"]),
            ): constraint["name"]
            for constraint in inspector.get_foreign_keys(table_name)
        }
        uniques = {
            tuple(constraint["column_names"]): constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        }
        shapes[table_name] = {"foreign_keys": foreign_keys, "uniques": uniques}
    return shapes


def _required_database() -> tuple[str, str]:
    required = os.environ.get("LAB_POSTGRES_REQUIRED", "").lower()
    database_url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    release_run_id = os.environ.get("LAB_RELEASE_RUN_ID", "")
    if required not in TRUE:
        pytest.skip("LAB_POSTGRES_REQUIRED was not requested")
    missing = [
        name
        for name, value in (
            ("LAB_TEST_DATABASE_URL", database_url),
            ("LAB_RELEASE_RUN_ID", release_run_id),
        )
        if not value
    ]
    if missing:
        pytest.fail("required migration environment is incomplete: " + ", ".join(missing))
    if make_url(database_url).drivername != "postgresql+asyncpg":
        pytest.fail("LAB_TEST_DATABASE_URL must use postgresql+asyncpg")
    return database_url, release_run_id


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )


@asynccontextmanager
async def _database_at_038():
    database_url, release_run_id = _required_database()
    source = create_async_engine(database_url)
    async with source.connect() as connection:
        database, disposable = (
            await connection.execute(
                text(
                    "SELECT current_database(), "
                    "current_setting('simverse.release_disposable', true)"
                )
            )
        ).one()
    await source.dispose()
    expected = f"simverse_lab_release_{release_run_id}"
    if database != expected or disposable != "on":
        pytest.fail(
            "migration source must be the asserted disposable release database: "
            f"database={database!r}, expected={expected!r}, disposable={disposable!r}"
        )

    database_name = f"simverse_lab_protocol_{uuid.uuid4().hex}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    control_url = make_url(database_url).set(database="postgres").render_as_string(
        hide_password=False
    )
    fresh_url = make_url(database_url).set(database=database_name).render_as_string(
        hide_password=False
    )
    control = create_async_engine(control_url, isolation_level="AUTOCOMMIT")
    created = False
    try:
        async with control.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            await connection.execute(
                text(
                    f'ALTER DATABASE "{database_name}" '
                    "SET simverse.release_disposable = 'on'"
                )
            )
        created = True
        env = {**os.environ, "DATABASE_URL": fresh_url, "DEBUG": "true"}
        migrated = _alembic(env, "upgrade", "038_add_lab_terminalization_v2")
        if migrated.returncode:
            pytest.fail(f"upgrade to 038 failed:\n{migrated.stdout}\n{migrated.stderr}")
        yield fresh_url, env
    finally:
        if created:
            async with control.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE "{database_name}" WITH (FORCE)')
                )
        await control.dispose()


async def test_protocol_migration_backfill_trigger_and_downgrade_guard():
    async with _database_at_038() as (database_url, env):
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO lab_runs "
                    "(id,task_id,researcher_slug,adapter,status,created_at) VALUES "
                    "('historic-run','historic-task','sage','simverse_ref','queued',"
                    "clock_timestamp())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_id,tenant_id,run_id,topic,payload_json,created_at) VALUES "
                    "('historic-enqueue','tenant','historic-run','lab.run.enqueue',"
                    "CAST('{\"run_id\":\"historic-run\",\"keep\":\"yes\"}' AS json),"
                    "clock_timestamp())"
                )
            )
        await engine.dispose()

        upgraded = _alembic(env, "upgrade", "039_add_lab_protocol_v2_state")
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            version, payload, default = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT protocol_version FROM lab_runs WHERE id='historic-run'), "
                        "(SELECT payload_json FROM outbox_events "
                        " WHERE event_id='historic-enqueue'), "
                        "(SELECT column_default FROM information_schema.columns "
                        " WHERE table_schema='public' AND table_name='lab_runs' "
                        " AND column_name='protocol_version')"
                    )
                )
            ).one()
        assert version == 1
        assert payload == {
            "run_id": "historic-run",
            "keep": "yes",
            "protocol_version": 1,
        }
        assert default is None

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(Exception, match="protocol_version is immutable"):
                await connection.execute(
                    text(
                        "UPDATE lab_runs SET protocol_version=2 "
                        "WHERE id='historic-run'"
                    )
                )
            await transaction.rollback()

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(Exception):
                await connection.execute(
                    text(
                        "INSERT INTO lab_runs "
                        "(id,task_id,researcher_slug,adapter,status,protocol_version,created_at) "
                        "VALUES ('invalid-run','invalid-task','sage','simverse_ref','queued',"
                        "3,clock_timestamp())"
                    )
                )
            await transaction.rollback()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO lab_runs "
                    "(id,task_id,researcher_slug,adapter,status,protocol_version,created_at) "
                    "VALUES ('v2-run','v2-task','sage','simverse_ref','queued',"
                    "2,clock_timestamp())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_id,tenant_id,run_id,topic,payload_json,created_at) VALUES "
                    "('v2-enqueue','tenant','v2-run','lab.run.enqueue',"
                    "CAST(:payload AS json),clock_timestamp())"
                ),
                {
                    "payload": json.dumps(
                        {"run_id": "v2-run", "protocol_version": 2}
                    )
                },
            )
        await engine.dispose()

        refused = _alembic(env, "downgrade", "038_add_lab_terminalization_v2")
        assert refused.returncode != 0
        assert "refusing Lab protocol-v2 downgrade" in refused.stderr
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            revision, v2_count = (
                await connection.execute(
                    text(
                        "SELECT (SELECT version_num FROM alembic_version), "
                        "(SELECT count(*) FROM lab_runs WHERE protocol_version=2)"
                    )
                )
            ).one()
            assert revision == "039_add_lab_protocol_v2_state"
            assert v2_count == 1
            await connection.execute(text("DELETE FROM lab_runs WHERE id='v2-run'"))
        await engine.dispose()

        residual_outbox = _alembic(
            env, "downgrade", "038_add_lab_terminalization_v2"
        )
        assert residual_outbox.returncode != 0
        assert "v2_enqueue_outbox=1" in residual_outbox.stderr
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM outbox_events WHERE event_id='v2-enqueue'")
            )
        await engine.dispose()

        clean = _alembic(env, "downgrade", "038_add_lab_terminalization_v2")
        assert clean.returncode == 0, f"{clean.stdout}\n{clean.stderr}"
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision, protocol_column, runtime_table, payload = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT version_num FROM alembic_version), "
                        "to_regclass('public.lab_runs') IS NOT NULL AND EXISTS ("
                        " SELECT 1 FROM information_schema.columns "
                        " WHERE table_schema='public' AND table_name='lab_runs' "
                        " AND column_name='protocol_version'), "
                        "to_regclass('public.lab_runtime_sessions'), "
                        "(SELECT payload_json FROM outbox_events "
                        " WHERE event_id='historic-enqueue')"
                    )
                )
            ).one()
        await engine.dispose()
        assert revision == "038_add_lab_terminalization_v2"
        assert protocol_column is False
        assert runtime_table is None
        assert payload["protocol_version"] == 1


async def test_protocol_migration_refuses_historical_enqueue_claiming_v2():
    async with _database_at_038() as (database_url, env):
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO lab_runs "
                    "(id,task_id,researcher_slug,adapter,status,created_at) VALUES "
                    "('historic-v2-run','historic-v2-task','sage','simverse_ref',"
                    "'queued',clock_timestamp())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(event_id,tenant_id,run_id,topic,payload_json,created_at) VALUES "
                    "('historic-v2-enqueue','tenant','historic-v2-run',"
                    "'lab.run.enqueue',CAST(:payload AS json),clock_timestamp())"
                ),
                {
                    "payload": json.dumps(
                        {"run_id": "historic-v2-run", "protocol_version": 2}
                    )
                },
            )
        await engine.dispose()

        refused = _alembic(env, "upgrade", "039_add_lab_protocol_v2_state")
        assert refused.returncode != 0
        assert "historical enqueue must be v1" in refused.stderr


async def test_runtime_intent_and_result_bindings_reject_cross_row_inserts():
    async with _database_at_038() as (database_url, env):
        upgraded = _alembic(env, "upgrade", "039_add_lab_protocol_v2_state")
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            migrated_shapes = await connection.run_sync(
                _migration_constraint_shapes
            )
        for table_name, model in RUNTIME_CONSTRAINT_MODELS.items():
            expected = _orm_constraint_shape(model)
            actual = migrated_shapes[table_name]
            assert actual["uniques"] == expected["uniques"]
        for table_name, model in RUNTIME_BINDING_MODELS.items():
            expected = _orm_constraint_shape(model)
            actual = migrated_shapes[table_name]
            assert actual["foreign_keys"].keys() == expected["foreign_keys"].keys()
            for binding, expected_name in expected["foreign_keys"].items():
                if expected_name is not None:
                    assert actual["foreign_keys"][binding] == expected_name

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO lab_runs "
                    "(id,task_id,researcher_slug,adapter,status,protocol_version,created_at) "
                    "VALUES "
                    "('binding-run-1','binding-task-1','sage','simverse_ref','queued',2,"
                    "clock_timestamp()),"
                    "('binding-run-2','binding-task-2','sage','simverse_ref','queued',2,"
                    "clock_timestamp())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_runtime_sessions "
                    "(id,run_id,client_run_id,fencing_epoch,protocol_version,provider_name,"
                    "durability_class,status,created_at,updated_at) VALUES "
                    "('binding-session-1','binding-run-1','binding-client-1',7,2,'ref',"
                    "'session_affine','ready',clock_timestamp(),clock_timestamp()),"
                    "('binding-session-2','binding-run-2','binding-client-2',8,2,'ref',"
                    "'session_affine','ready',clock_timestamp(),clock_timestamp())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_runtime_turns "
                    "(id,session_id,turn_id,sequence,status,created_at,updated_at) VALUES "
                    "('binding-turn-1','binding-session-1','turn-1',1,'intent_pending',"
                    "clock_timestamp(),clock_timestamp()),"
                    "('binding-turn-2','binding-session-2','turn-2',1,'intent_pending',"
                    "clock_timestamp(),clock_timestamp())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_runtime_intents "
                    "(id,session_id,runtime_turn_id,intent_id,action_id,tool_name,args_digest,"
                    "status,provider_cursor,fencing_epoch,created_at,updated_at) VALUES "
                    "('binding-intent-1','binding-session-1','binding-turn-1','intent-1',"
                    "'action-1','web.search',:digest,'pending',1,7,clock_timestamp(),"
                    "clock_timestamp()),"
                    "('binding-intent-2','binding-session-2','binding-turn-2','intent-2',"
                    "'action-2','web.search',:digest,'pending',1,8,clock_timestamp(),"
                    "clock_timestamp())"
                ),
                {"digest": "a" * 64},
            )

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO lab_runtime_intents "
                        "(id,session_id,runtime_turn_id,intent_id,action_id,tool_name,"
                        "args_digest,status,provider_cursor,fencing_epoch,created_at,updated_at) "
                        "VALUES ('cross-session-intent','binding-session-1','binding-turn-2',"
                        "'cross-intent','cross-action','web.search',:digest,'pending',2,7,"
                        "clock_timestamp(),clock_timestamp())"
                    ),
                    {"digest": "b" * 64},
                )
            await transaction.rollback()

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO lab_runtime_intents "
                        "(id,session_id,runtime_turn_id,intent_id,action_id,tool_name,"
                        "args_digest,status,provider_cursor,fencing_epoch,created_at,updated_at) "
                        "VALUES ('cross-epoch-intent','binding-session-1','binding-turn-1',"
                        "'epoch-intent','epoch-action','web.search',:digest,'pending',2,8,"
                        "clock_timestamp(),clock_timestamp())"
                    ),
                    {"digest": "e" * 64},
                )
            await transaction.rollback()

        base_result = {
            "session_id": "binding-session-1",
            "runtime_turn_id": "binding-turn-1",
            "runtime_intent_id": "binding-intent-1",
            "intent_id": "intent-1",
            "action_id": "action-1",
            "fencing_epoch": 7,
        }
        mutations = {
            "session": {"session_id": "binding-session-2"},
            "turn": {"runtime_turn_id": "binding-turn-2"},
            "runtime-intent": {"runtime_intent_id": "binding-intent-2"},
            "intent": {"intent_id": "intent-2"},
            "action": {"action_id": "action-2"},
            "epoch": {"fencing_epoch": 8},
        }
        for label, override in mutations.items():
            values = {**base_result, **override, "label": label}
            async with engine.connect() as connection:
                transaction = await connection.begin()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "INSERT INTO lab_runtime_results "
                            "(id,session_id,runtime_turn_id,runtime_intent_id,intent_id,"
                            "action_id,command_id,receipt_id,outcome,request_digest,"
                            "result_digest,payload_json,fencing_epoch,created_at) VALUES "
                            "(:label,:session_id,:runtime_turn_id,:runtime_intent_id,"
                            ":intent_id,:action_id,:command_id,:receipt_id,'succeeded',"
                            ":digest,:digest,CAST('{}' AS json),:fencing_epoch,"
                            "clock_timestamp())"
                        ),
                        {
                            **values,
                            "command_id": f"command-{label}",
                            "receipt_id": f"receipt-{label}",
                            "digest": "c" * 64,
                        },
                    )
                await transaction.rollback()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO lab_runtime_results "
                    "(id,session_id,runtime_turn_id,runtime_intent_id,intent_id,action_id,"
                    "command_id,receipt_id,outcome,request_digest,result_digest,payload_json,"
                    "fencing_epoch,created_at) VALUES "
                    "('binding-result-1','binding-session-1','binding-turn-1',"
                    "'binding-intent-1','intent-1','action-1','binding-command-1',"
                    "'binding-receipt-1','succeeded',:digest,:digest,CAST('{}' AS json),7,"
                    "clock_timestamp())"
                ),
                {"digest": "d" * 64},
            )
        await engine.dispose()
