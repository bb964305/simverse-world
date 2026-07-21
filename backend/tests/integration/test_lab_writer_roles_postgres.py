"""Real-PostgreSQL proof for the Lab financial writer role boundary."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


pytestmark = [pytest.mark.lab_postgres, pytest.mark.anyio]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
TRUE = {"1", "true", "yes", "on"}


def _required_database() -> tuple[str, str]:
    required = os.environ.get("LAB_POSTGRES_REQUIRED", "").lower()
    database_url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    run_id = os.environ.get("LAB_RELEASE_RUN_ID", "")
    if required not in TRUE or not database_url or not run_id:
        pytest.fail(
            "AC04 requires LAB_POSTGRES_REQUIRED=true, LAB_TEST_DATABASE_URL, "
            "and LAB_RELEASE_RUN_ID"
        )
    if make_url(database_url).drivername != "postgresql+asyncpg":
        pytest.fail("LAB_TEST_DATABASE_URL must use postgresql+asyncpg")
    return database_url, run_id


@pytest.fixture(scope="module")
def migrated_database() -> tuple[str, str]:
    database_url, run_id = _required_database()
    env = {**os.environ, "DATABASE_URL": database_url, "DEBUG": "true"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        pytest.fail(f"AC04 migration failed:\n{result.stdout}\n{result.stderr}")
    return database_url, run_id


@asynccontextmanager
async def _fresh_migrated_database(database_url: str):
    database_name = f"simverse_lab_downgrade_{uuid.uuid4().hex}"
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
        created = True
        env = {**os.environ, "DATABASE_URL": fresh_url, "DEBUG": "true"}
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "038_add_lab_terminalization_v2"],
            cwd=BACKEND_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            pytest.fail(f"fresh migration failed:\n{result.stdout}\n{result.stderr}")
        yield fresh_url, env
    finally:
        if created:
            async with control.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE "{database_name}" WITH (FORCE)')
                )
        await control.dispose()


async def _assert_denied(database_url: str, statement: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            with pytest.raises(Exception, match="permission denied|not permitted"):
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _call_breakglass(connection, request: dict[str, object]) -> str:
    return (
        await connection.execute(
            text(
                "SELECT public.apply_lab_breakglass_compensation("
                ":operation_key, :ticket, :reason, :actor, :task_id, :hold_id, "
                "CAST(:legs AS jsonb))"
            ),
            {**request, "legs": json.dumps(request["legs"], sort_keys=True)},
        )
    ).scalar_one()


async def _breakglass_snapshot(admin, case: dict[str, str]) -> tuple:
    async with admin.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT target.soul_coin_balance, treasury.balance_sc, "
                    "hold.status, task.status, "
                    "(SELECT count(*) FROM lab_breakglass_audits "
                    " WHERE task_id=:task) AS audits, "
                    "(SELECT count(*) FROM lab_compensation_entries "
                    " WHERE task_id=:task) AS compensation_entries, "
                    "(SELECT count(*) FROM transactions "
                    " WHERE user_id=:target AND reason LIKE 'lab_compensation:%') "
                    " AS transactions, "
                    "(SELECT count(*) FROM outbox_events "
                    " WHERE payload_json::jsonb->>'task_id'=:task "
                    " AND payload_json::jsonb->>'type'='lab.finance.compensated') "
                    " AS outbox_events, "
                    "(SELECT count(*) FROM coin_hold_entries WHERE hold_id=:hold) "
                    " AS hold_entries, "
                    "(SELECT count(*) FROM lab_terminalization_receipts WHERE hold_id=:hold) "
                    " AS receipts "
                    "FROM users target "
                    "JOIN resident_treasuries treasury ON treasury.resident_slug=:treasury "
                    "JOIN coin_holds hold ON hold.id=:hold "
                    "JOIN lab_tasks task ON task.id=:task "
                    "WHERE target.id=:target"
                ),
                case,
            )
        ).one()


@asynccontextmanager
async def _activated_breakglass(database_url: str, run_id: str):
    admin = create_async_engine(database_url)
    suffix = re.sub(r"[^a-z0-9_]", "_", run_id.lower())[:20] + uuid.uuid4().hex[:8]
    operator_role = f"lab_breakglass_probe_{suffix}"
    operator_password = f"breakglass-{uuid.uuid4().hex}"
    operator_url = make_url(database_url).set(
        username=operator_role,
        password=operator_password,
    ).render_as_string(hide_password=False)
    signature = (
        "public.apply_lab_breakglass_compensation("
        "text,text,text,text,text,text,jsonb)"
    )
    role_created = False
    operator = None
    try:
        async with admin.begin() as connection:
            database = (
                await connection.execute(text("SELECT current_database()"))
            ).scalar_one()
            default_boundary = (
                await connection.execute(
                    text(
                        "SELECT "
                        "has_function_privilege('public', :signature, 'EXECUTE') "
                        "AS public_execute, "
                        "has_function_privilege('lab_terminalizer_v2', :signature, "
                        "'EXECUTE') AS terminalizer_execute, "
                        "has_function_privilege('lab_terminalizer_breakglass', "
                        ":signature, 'EXECUTE') AS breakglass_execute, "
                        "owner.rolname AS owner, proc.prosecdef, proc.proconfig "
                        "FROM pg_proc proc "
                        "JOIN pg_roles owner ON owner.oid = proc.proowner "
                        "WHERE proc.oid = CAST(:signature AS regprocedure)"
                    ),
                    {"signature": signature},
                )
            ).one()
            assert default_boundary.public_execute is False
            assert default_boundary.terminalizer_execute is False
            assert default_boundary.breakglass_execute is False
            assert default_boundary.owner == "lab_financial_kernel_owner"
            assert default_boundary.prosecdef is True
            assert default_boundary.proconfig == ["search_path=pg_catalog, public"]
            await connection.execute(
                text(
                    f"CREATE ROLE {operator_role} LOGIN PASSWORD '{operator_password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                )
            )
            role_created = True
            await connection.execute(
                text(f"GRANT CONNECT ON DATABASE {database} TO {operator_role}")
            )
            await connection.execute(
                text(f"GRANT lab_terminalizer_breakglass TO {operator_role}")
            )
            await connection.execute(
                text("GRANT USAGE ON SCHEMA public TO lab_terminalizer_breakglass")
            )
            await connection.execute(
                text(
                    "GRANT EXECUTE ON FUNCTION "
                    f"{signature} TO lab_terminalizer_breakglass"
                )
            )

        operator = create_async_engine(operator_url)
        yield admin, operator
    finally:
        if operator is not None:
            await operator.dispose()
        if role_created:
            async with admin.begin() as connection:
                await connection.execute(
                    text(
                        "REVOKE EXECUTE ON FUNCTION "
                        f"{signature} FROM lab_terminalizer_breakglass"
                    )
                )
                await connection.execute(
                    text("REVOKE USAGE ON SCHEMA public FROM lab_terminalizer_breakglass")
                )
                await connection.execute(text(f"DROP OWNED BY {operator_role} CASCADE"))
                await connection.execute(text(f"DROP ROLE {operator_role}"))
        await admin.dispose()


async def test_writer_roles_and_controlled_entrypoint(migrated_database):
    database_url, run_id = migrated_database
    admin = create_async_engine(database_url)
    suffix = re.sub(r"[^a-z0-9_]", "_", run_id.lower())[:24] + uuid.uuid4().hex[:8]
    api_role = f"lab_api_probe_{suffix}"
    api_password = f"api-{uuid.uuid4().hex}"
    terminalizer_password = f"term-{uuid.uuid4().hex}"
    api_url = make_url(database_url).set(
        username=api_role, password=api_password
    ).render_as_string(hide_password=False)
    terminalizer_url = make_url(database_url).set(
        username="lab_terminalizer_v2", password=terminalizer_password
    ).render_as_string(
        hide_password=False
    )
    task_id = f"role-task-{uuid.uuid4().hex}"
    lab_run_id = str(uuid.uuid4())
    hold_id = f"role-hold-{uuid.uuid4().hex}"
    issuer_id = f"role-issuer-{uuid.uuid4().hex}"
    recipient_id = f"role-recipient-{uuid.uuid4().hex}"
    researcher_slug = f"role-researcher-{uuid.uuid4().hex}"
    command_id = ""
    receipt_id = ""
    event_id = ""

    try:
        async with admin.begin() as connection:
            database, disposable = (
                await connection.execute(
                    text(
                        "SELECT current_database(), "
                        "current_setting('simverse.release_disposable', true)"
                    )
                )
            ).one()
            assert database == f"simverse_lab_release_{run_id}"
            assert disposable == "on"

            roles = {
                row.rolname: row
                for row in (
                    await connection.execute(
                        text(
                            "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, "
                            "rolcreatedb, rolinherit FROM pg_roles WHERE rolname IN "
                            "('lab_financial_kernel_owner', "
                            "'lab_command_submitter_v2', 'lab_terminalizer_v2', "
                            "'lab_terminalizer_breakglass')"
                        )
                    )
                )
            }
            assert set(roles) == {
                "lab_financial_kernel_owner",
                "lab_command_submitter_v2",
                "lab_terminalizer_v2",
                "lab_terminalizer_breakglass",
            }
            assert roles["lab_financial_kernel_owner"].rolcanlogin is False
            assert roles["lab_command_submitter_v2"].rolcanlogin is False
            assert roles["lab_terminalizer_v2"].rolcanlogin is True
            assert roles["lab_terminalizer_breakglass"].rolcanlogin is False
            assert all(
                not row.rolsuper and not row.rolcreaterole and not row.rolcreatedb
                for row in roles.values()
            )
            memberships = {
                row.role_name: row.is_member
                for row in (
                    await connection.execute(
                        text(
                            "SELECT role_name, pg_has_role(role_name, "
                            "'lab_financial_kernel_owner', 'MEMBER') AS is_member "
                            "FROM (VALUES ('lab_command_submitter_v2'), "
                            "('lab_terminalizer_v2'), "
                            "('lab_terminalizer_breakglass')) roles(role_name)"
                        )
                    )
                )
            }
            assert memberships == {
                "lab_command_submitter_v2": False,
                "lab_terminalizer_v2": False,
                "lab_terminalizer_breakglass": False,
            }

            function = (
                await connection.execute(
                    text(
                        "SELECT owner.rolname AS owner, proc.prosecdef, proc.proconfig, "
                        "oidvectortypes(proc.proargtypes) AS args, "
                        "pg_get_function_result(proc.oid) AS result "
                        "FROM pg_proc proc JOIN pg_roles owner ON owner.oid = proc.proowner "
                        "WHERE proc.oid = "
                        "'public.finalize_lab_terminalization(text,bigint)'::regprocedure"
                    )
                )
            ).one()
            assert function.owner == "lab_financial_kernel_owner"
            assert function.prosecdef is True
            assert function.proconfig == ["search_path=pg_catalog, public"]
            assert function.args == "text, bigint"
            assert function.result == "text"
            public_execute = (
                await connection.execute(
                    text(
                        "SELECT has_function_privilege('public', "
                        "'public.finalize_lab_terminalization(text,bigint)', 'EXECUTE')"
                    )
                )
            ).scalar_one()
            assert public_execute is False

            submit_function = (
                await connection.execute(
                    text(
                        "SELECT owner.rolname AS owner, proc.prosecdef, proc.proconfig, "
                        "oidvectortypes(proc.proargtypes) AS args, "
                        "pg_get_function_result(proc.oid) AS result, "
                        "has_function_privilege('public', proc.oid, 'EXECUTE') "
                        "AS public_execute, "
                        "has_function_privilege('lab_command_submitter_v2', "
                        "proc.oid, 'EXECUTE') AS submitter_execute, "
                        "has_function_privilege('lab_terminalizer_v2', proc.oid, "
                        "'EXECUTE') AS terminalizer_execute "
                        "FROM pg_proc proc JOIN pg_roles owner ON owner.oid = proc.proowner "
                        "WHERE proc.oid = "
                        "'public.submit_lab_terminalization_command(text,text,text,bigint)'"
                        "::regprocedure"
                    )
                )
            ).one()
            assert submit_function.owner == "lab_financial_kernel_owner"
            assert submit_function.prosecdef is True
            assert submit_function.proconfig == ["search_path=pg_catalog, public"]
            assert submit_function.args == "text, text, text, bigint"
            assert submit_function.result == "text"
            assert submit_function.public_execute is False
            assert submit_function.submitter_execute is True
            assert submit_function.terminalizer_execute is False

            await connection.execute(
                text(
                    f"CREATE ROLE {api_role} LOGIN PASSWORD '{api_password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                )
            )
            await connection.execute(
                text(f"GRANT CONNECT ON DATABASE {database} TO {api_role}")
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {api_role}"))
            await connection.execute(
                text(f"GRANT lab_command_submitter_v2 TO {api_role}")
            )
            await connection.execute(
                text(
                    f"ALTER ROLE lab_terminalizer_v2 PASSWORD '{terminalizer_password}'"
                )
            )

            now = "clock_timestamp()"
            await connection.execute(
                text(
                    f"INSERT INTO users(id,name,email,soul_coin_balance,created_at) VALUES "
                    f"(:issuer,'issuer',:issuer_email,0,{now}),"
                    f"(:recipient,'recipient',:recipient_email,0,{now})"
                ),
                {
                    "issuer": issuer_id,
                    "issuer_email": f"{issuer_id}@role.test",
                    "recipient": recipient_id,
                    "recipient_email": f"{recipient_id}@role.test",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO residents(id,slug,name,creator_id,created_at) "
                    f"VALUES (:id,:slug,'role researcher',:creator,{now})"
                ),
                {
                    "id": f"role-resident-{uuid.uuid4().hex}",
                    "slug": researcher_slug,
                    "creator": recipient_id,
                },
            )
            await connection.execute(
                text(
                    f"INSERT INTO coin_holds(id,user_id,amount,reason,status,created_at,"
                    f"terminalization_version,cutover_at) VALUES "
                    f"(:hold,:issuer,10,:reason,'held',{now},'v2',{now})"
                ),
                {"hold": hold_id, "issuer": issuer_id, "reason": f"lab_task:{task_id}"},
            )
            await connection.execute(
                text(
                    f"INSERT INTO lab_tasks(id,issuer_user_id,title,brief_md,reward_sc,"
                    "platform_fee_sc,terminal_creator_share_bps,researcher_slug,"
                    "deliverable_kind,status,hold_id,reject_count,"
                    f"accepted_run_id,deadline_at,created_at,updated_at) VALUES "
                    f"(:task,:issuer,'role probe','',10,0,10000,:researcher,"
                    f"'report','review',:hold,0,:run,"
                    f"{now} + interval '1 day',{now},{now})"
                ),
                {
                    "task": task_id,
                    "issuer": issuer_id,
                    "hold": hold_id,
                    "run": lab_run_id,
                    "researcher": researcher_slug,
                },
            )
            await connection.execute(
                text(
                        "INSERT INTO lab_runs(id,task_id,researcher_slug,status,"
                        "protocol_version,created_at) "
                        f"VALUES (:run,:task,:researcher,'succeeded',1,{now})"
                ),
                {"run": lab_run_id, "task": task_id, "researcher": researcher_slug},
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_run_leases(run_id,owner_id,fencing_epoch,heartbeat_at,"
                    f"expires_at,created_at,updated_at) VALUES "
                    f"(:run,'role-owner',0,{now},{now} + interval '5 minutes',{now},{now})"
                ),
                {"run": lab_run_id},
            )

        await _assert_denied(
            api_url,
            "INSERT INTO lab_terminalization_commands(command_id,operation,task_id,"
            "hold_id,actor,expected_epoch,idempotency_key,status,payload_json,created_at) "
            "VALUES ('raw','accept','raw','raw','raw',0,'raw','pending','{}',"
            "clock_timestamp())",
        )
        await _assert_denied(
            api_url,
            "SELECT public.submit_lab_terminalization_command("
            "'accept','missing','missing',0)",
        )

        api = create_async_engine(api_url)
        try:
            async with api.begin() as connection:
                await connection.execute(
                    text("SET LOCAL ROLE lab_command_submitter_v2")
                )
                command_id = (
                    await connection.execute(
                        text(
                            "SELECT public.submit_lab_terminalization_command("
                            "'accept', :task, :actor, 0)"
                        ),
                        {"task": task_id, "actor": issuer_id},
                    )
                ).scalar_one()
        finally:
            await api.dispose()

        async with admin.connect() as connection:
            event_id, receipt_id = (
                await connection.execute(
                    text(
                        "SELECT payload_json::jsonb->>'event_id', "
                        "payload_json::jsonb->>'receipt_id' "
                        "FROM lab_terminalization_commands WHERE command_id=:command"
                    ),
                    {"command": command_id},
                )
            ).one()

        await _assert_denied(api_url, "UPDATE coin_holds SET status='settled'")
        await _assert_denied(
            api_url, "SELECT public.finalize_lab_terminalization('missing', 0)"
        )
        await _assert_denied(
            terminalizer_url, "UPDATE coin_holds SET status='settled'"
        )
        await _assert_denied(
            terminalizer_url, "SET ROLE lab_financial_kernel_owner"
        )
        await _assert_denied(
            terminalizer_url, "SELECT * FROM lab_terminalization_commands"
        )

        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE lab_terminalization_commands SET payload_json = "
                    "jsonb_set(payload_json::jsonb, '{splits,0,recipient_key}', "
                    "to_jsonb(CAST(:recipient AS text))) WHERE command_id=:command"
                ),
                {"recipient": issuer_id, "command": command_id},
            )

        terminalizer = create_async_engine(terminalizer_url)
        try:
            with pytest.raises(DBAPIError, match="not canonical"):
                async with terminalizer.begin() as connection:
                    await connection.execute(
                        text("SELECT public.finalize_lab_terminalization(:command, 0)"),
                        {"command": command_id},
                    )

            async with admin.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE lab_terminalization_commands SET payload_json = "
                        "public.canonical_lab_terminalization_payload("
                        "operation, task_id, hold_id, expected_epoch) "
                        "WHERE command_id=:command"
                    ),
                    {"command": command_id},
                )

            async with terminalizer.begin() as connection:
                actual = (
                    await connection.execute(
                        text("SELECT public.finalize_lab_terminalization(:command, 0)"),
                        {"command": command_id},
                    )
                ).scalar_one()
                repeated = (
                    await connection.execute(
                        text("SELECT public.finalize_lab_terminalization(:command, 0)"),
                        {"command": command_id},
                    )
                ).scalar_one()
            assert actual == receipt_id
            assert repeated == receipt_id
        finally:
            await terminalizer.dispose()

        async with admin.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        "SELECT hold.status AS hold_status, task.status AS task_status, "
                        "recipient.soul_coin_balance AS balance, "
                        "(SELECT count(*) FROM coin_hold_entries WHERE hold_id=:hold) AS entries, "
                        "(SELECT count(*) FROM outbox_events WHERE event_id=:event) AS events, "
                        "(SELECT count(*) FROM lab_terminalization_receipts "
                        " WHERE command_id=:command) AS receipts, "
                        "(SELECT topic FROM outbox_events WHERE event_id=:event) AS topic "
                        "FROM coin_holds hold JOIN lab_tasks task ON task.hold_id=hold.id "
                        "JOIN users recipient ON recipient.id=:recipient WHERE hold.id=:hold"
                    ),
                    {
                        "hold": hold_id,
                        "event": event_id,
                        "command": command_id,
                        "recipient": recipient_id,
                    },
                )
            ).one()
            assert state == (
                "settled",
                "completed",
                10,
                1,
                1,
                1,
                "lab.task.terminalized",
            )
    finally:
        async with admin.begin() as connection:
            role_exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname=:role"),
                    {"role": api_role},
                )
            ).scalar_one_or_none()
            if role_exists:
                await connection.execute(text(f"DROP OWNED BY {api_role} CASCADE"))
                await connection.execute(text(f"DROP ROLE {api_role}"))
        await admin.dispose()


async def test_breakglass_compensation_is_balanced_audited_and_default_denied(
    migrated_database,
):
    database_url, run_id = migrated_database
    prefix = uuid.uuid4().hex
    case = {
        "issuer": f"comp-issuer-{prefix}",
        "target": f"comp-target-{prefix}",
        "treasury": f"comp-treasury-{prefix}",
        "task": f"comp-task-{prefix}",
        "hold": f"comp-hold-{prefix}",
    }
    now = "clock_timestamp()"

    async with _activated_breakglass(database_url, run_id) as (admin, operator):
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO users(id,name,email,soul_coin_balance,created_at) VALUES "
                    f"(:issuer,'issuer',:issuer_email,0,{now}),"
                    f"(:target,'target',:target_email,10,{now})"
                ),
                {
                    **case,
                    "issuer_email": f"{case['issuer']}@comp.test",
                    "target_email": f"{case['target']}@comp.test",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO resident_treasuries(resident_slug,balance_sc,updated_at) "
                    f"VALUES (:treasury,8,{now})"
                ),
                case,
            )
            await connection.execute(
                text(
                    "INSERT INTO coin_holds(id,user_id,amount,reason,status,created_at,"
                    "terminalization_version,cutover_at) VALUES "
                    f"(:hold,:issuer,20,:hold_reason,'held',{now},'v1',NULL)"
                ),
                {**case, "hold_reason": f"lab_task:{case['task']}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_tasks(id,issuer_user_id,title,brief_md,reward_sc,"
                    "platform_fee_sc,deliverable_kind,status,hold_id,reject_count,"
                    "deadline_at,created_at,updated_at) VALUES "
                    f"(:task,:issuer,'compensation probe','',20,0,'report','funded',"
                    f":hold,0,{now} + interval '1 day',{now},{now})"
                ),
                case,
            )

        request = {
            "operation_key": f"compensate:{prefix}",
            "ticket": f"TEST-{prefix[:12]}",
            "reason": "correct an injected settlement discrepancy",
            "actor": "operator:release-test",
            "task_id": case["task"],
            "hold_id": case["hold"],
            "legs": [
                {"recipient_key": case["target"], "amount_delta": 5},
                {
                    "recipient_key": f"treasury:{case['treasury']}",
                    "amount_delta": 3,
                },
                {"recipient_key": "sink", "amount_delta": -8},
            ],
        }
        before = await _breakglass_snapshot(admin, case)

        async with operator.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE lab_terminalizer_breakglass"))
            audit_id = await _call_breakglass(connection, request)
        after = await _breakglass_snapshot(admin, case)
        assert after == (15, 11, "held", "funded", 1, 3, 1, 1, 0, 0)

        async with operator.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE lab_terminalizer_breakglass"))
            assert await _call_breakglass(connection, request) == audit_id
        assert await _breakglass_snapshot(admin, case) == after

        changed = {**request, "actor": "operator:different"}
        async with operator.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE lab_terminalizer_breakglass"))
            with pytest.raises(DBAPIError, match="operation key binding"):
                await _call_breakglass(connection, changed)
        assert await _breakglass_snapshot(admin, case) == after

        concurrent = {
            **request,
            "operation_key": f"compensate-concurrent:{prefix}",
            "legs": [
                {"recipient_key": case["target"], "amount_delta": 1},
                {"recipient_key": "sink", "amount_delta": -1},
            ],
        }
        start = asyncio.Event()

        async def invoke_once() -> str:
            async with operator.begin() as connection:
                await connection.execute(
                    text("SET LOCAL ROLE lab_terminalizer_breakglass")
                )
                await start.wait()
                return await _call_breakglass(connection, concurrent)

        calls = [asyncio.create_task(invoke_once()) for _ in range(2)]
        start.set()
        concurrent_ids = await asyncio.gather(*calls)
        assert concurrent_ids[0] == concurrent_ids[1]
        concurrent_state = await _breakglass_snapshot(admin, case)
        assert concurrent_state == (16, 11, "held", "funded", 2, 5, 2, 2, 0, 0)

        invalid_legs = [
            [{"recipient_key": case["target"], "amount_delta": 1}],
            [
                {"recipient_key": case["target"], "amount_delta": 1},
                {"recipient_key": case["target"], "amount_delta": -1},
            ],
            [
                {"recipient_key": case["target"], "amount_delta": "1"},
                {"recipient_key": "sink", "amount_delta": -1},
            ],
            [
                {"recipient_key": f"missing-{prefix}", "amount_delta": 1},
                {"recipient_key": "sink", "amount_delta": -1},
            ],
            [
                {"recipient_key": case["target"], "amount_delta": -100},
                {"recipient_key": "sink", "amount_delta": 100},
            ],
            [
                {"recipient_key": case["target"], "amount_delta": 21},
                {"recipient_key": "sink", "amount_delta": -21},
            ],
        ]
        for index, legs in enumerate(invalid_legs):
            invalid = {
                **request,
                "operation_key": f"invalid:{index}:{prefix}",
                "legs": legs,
            }
            async with operator.begin() as connection:
                await connection.execute(
                    text("SET LOCAL ROLE lab_terminalizer_breakglass")
                )
                with pytest.raises(DBAPIError):
                    await _call_breakglass(connection, invalid)
            assert await _breakglass_snapshot(admin, case) == concurrent_state

        fault_points = [
            "breakglass:after_audit",
            f"breakglass:after_balance:{case['target']}",
            f"breakglass:after_ledger:{case['target']}",
            "breakglass:after_balance:sink",
            "breakglass:after_ledger:sink",
            "breakglass:after_outbox",
            "breakglass:before_commit",
        ]
        for index, point in enumerate(fault_points):
            fault_request = {
                **request,
                "operation_key": f"fault:{index}:{prefix}",
                "legs": [
                    {"recipient_key": case["target"], "amount_delta": 1},
                    {"recipient_key": "sink", "amount_delta": -1},
                ],
            }
            async with operator.begin() as connection:
                await connection.execute(
                    text("SET LOCAL ROLE lab_terminalizer_breakglass")
                )
                await connection.execute(
                    text(
                        "SELECT set_config("
                        "'simverse.lab_terminalization_fault', :point, true)"
                    ),
                    {"point": point},
                )
                with pytest.raises(DBAPIError, match="injected Lab terminalization fault"):
                    await _call_breakglass(connection, fault_request)
            assert await _breakglass_snapshot(admin, case) == concurrent_state

        for statement in (
            "UPDATE lab_breakglass_audits SET reason='tampered'",
            "DELETE FROM lab_compensation_entries",
        ):
            async with operator.begin() as connection:
                await connection.execute(
                    text("SET LOCAL ROLE lab_terminalizer_breakglass")
                )
                with pytest.raises(DBAPIError, match="permission denied"):
                    await connection.execute(text(statement))
        for statement in (
            "UPDATE lab_breakglass_audits SET reason='tampered'",
            "DELETE FROM lab_compensation_entries",
        ):
            async with admin.begin() as connection:
                with pytest.raises(DBAPIError, match="append-only"):
                    await connection.execute(text(statement))

        assert before == (10, 8, "held", "funded", 0, 0, 0, 0, 0, 0)

    admin = create_async_engine(database_url)
    try:
        async with admin.connect() as connection:
            still_granted = (
                await connection.execute(
                    text(
                        "SELECT has_function_privilege("
                        "'lab_terminalizer_breakglass', "
                        "'public.apply_lab_breakglass_compensation("
                        "text,text,text,text,text,text,jsonb)', 'EXECUTE')"
                    )
                )
            ).scalar_one()
        assert still_granted is False
    finally:
        await admin.dispose()


async def test_failure_record_waits_for_command_owner_and_preserves_completion(
    migrated_database,
):
    from app.lab import terminalizer

    database_url, _ = migrated_database
    prefix = f"failure-race-{uuid.uuid4().hex}"
    user_id = f"{prefix}-user"
    task_id = f"{prefix}-task"
    hold_id = f"{prefix}-hold"
    command_id = f"{prefix}-command"
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    failure = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users(id,name,email,soul_coin_balance,created_at) "
                    "VALUES (:user,'failure race',:email,0,clock_timestamp())"
                ),
                {"user": user_id, "email": f"{user_id}@test.invalid"},
            )
            await connection.execute(
                text(
                    "INSERT INTO coin_holds(id,user_id,amount,reason,status,created_at,"
                    "terminalization_version) VALUES ("
                    ":hold,:user,1,:reason,'held',clock_timestamp(),'v1')"
                ),
                {"hold": hold_id, "user": user_id, "reason": f"lab_task:{task_id}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_tasks(id,issuer_user_id,title,brief_md,reward_sc,"
                    "platform_fee_sc,deliverable_kind,status,hold_id,reject_count,"
                    "deadline_at,created_at,updated_at) VALUES ("
                    ":task,:user,'failure race','',1,0,'report','funded',:hold,0,"
                    "clock_timestamp() + interval '1 day',clock_timestamp(),"
                    "clock_timestamp())"
                ),
                {"task": task_id, "user": user_id, "hold": hold_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO lab_terminalization_commands("
                    "command_id,operation,task_id,hold_id,actor,expected_epoch,"
                    "idempotency_key,status,attempts,payload_json,created_at) VALUES ("
                    ":command,'cancel',:task,:hold,:actor,0,:idempotency,'pending',0,"
                    "CAST('{}' AS json),clock_timestamp())"
                ),
                {
                    "command": command_id,
                    "task": task_id,
                    "hold": hold_id,
                    "actor": user_id,
                    "idempotency": f"failure-race:{command_id}",
                },
            )

        async with engine.connect() as owner:
            transaction = await owner.begin()
            await owner.execute(
                text("SELECT id FROM lab_tasks WHERE id=:task FOR UPDATE"),
                {"task": task_id},
            )
            await owner.execute(
                text("SELECT id FROM coin_holds WHERE id=:hold FOR UPDATE"),
                {"hold": hold_id},
            )
            failure = asyncio.create_task(
                terminalizer._record_failure(
                    factory,
                    command_id=command_id,
                    task_id=task_id,
                    run_id=None,
                    exc=RuntimeError("stale failure"),
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(failure), timeout=0.2)
            await owner.execute(
                text(
                    "SELECT command_id FROM lab_terminalization_commands "
                    "WHERE command_id=:command FOR UPDATE"
                ),
                {"command": command_id},
            )
            await owner.execute(
                text(
                    "UPDATE lab_terminalization_commands SET status='completed', "
                    "completed_at=clock_timestamp() WHERE command_id=:command"
                ),
                {"command": command_id},
            )
            await transaction.commit()

        assert await asyncio.wait_for(failure, timeout=2) == "ignored"
        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        "SELECT status, attempts, last_error "
                        "FROM lab_terminalization_commands WHERE command_id=:command"
                    ),
                    {"command": command_id},
                )
            ).one()
        assert state == ("completed", 0, None)
    finally:
        if failure is not None and not failure.done():
            failure.cancel()
            await asyncio.gather(failure, return_exceptions=True)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM lab_terminalization_commands WHERE command_id=:command"
                ),
                {"command": command_id},
            )
            await connection.execute(
                text("DELETE FROM lab_tasks WHERE id=:task"), {"task": task_id}
            )
            await connection.execute(
                text("DELETE FROM coin_holds WHERE id=:hold"), {"hold": hold_id}
            )
            await connection.execute(
                text("DELETE FROM users WHERE id=:user"), {"user": user_id}
            )
        await engine.dispose()


async def test_downgrade_serializes_with_v1_history_then_allows_clean_schema(
    migrated_database,
):
    database_url, _ = migrated_database
    command_id = f"downgrade-command-{uuid.uuid4().hex}"
    async with _fresh_migrated_database(database_url) as (fresh_url, env):
        engine = create_async_engine(fresh_url)
        writer = await engine.connect()
        writer_transaction = await writer.begin()
        process = None
        try:
            await writer.execute(
                text(
                    "INSERT INTO lab_terminalization_commands("
                    "command_id,operation,task_id,hold_id,actor,expected_epoch,"
                    "idempotency_key,status,attempts,payload_json,created_at) VALUES ("
                    ":command,'cancel','downgrade-task','downgrade-v1-hold',"
                    "'downgrade-actor',0,"
                    ":idempotency,'pending',0,CAST('{}' AS json),clock_timestamp())"
                ),
                {
                    "command": command_id,
                    "idempotency": f"downgrade:{command_id}",
                },
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "downgrade",
                "037_add_lab_worker_attempts",
                cwd=BACKEND_ROOT,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            blocked = False
            for _ in range(100):
                if process.returncode is not None:
                    break
                async with engine.connect() as observer:
                    blocked = bool(
                        await observer.scalar(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                                "AND query LIKE 'LOCK TABLE coin_holds,%' "
                                "AND wait_event_type='Lock')"
                            )
                        )
                    )
                if blocked:
                    break
                await asyncio.sleep(0.05)
            assert blocked, "downgrade did not block behind the active history writer"

            await writer_transaction.commit()
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            assert process.returncode != 0, stdout.decode()
            error = stderr.decode()
            assert "refusing Lab terminalization downgrade" in error
            assert "v2_holds=0" in error
            assert "commands=1" in error

            async with engine.begin() as connection:
                revision, command_count = (
                    await connection.execute(
                        text(
                            "SELECT (SELECT version_num FROM alembic_version), "
                            "(SELECT count(*) FROM lab_terminalization_commands)"
                        )
                    )
                ).one()
                assert revision == "038_add_lab_terminalization_v2"
                assert command_count == 1
                await connection.execute(
                    text(
                        "DELETE FROM lab_terminalization_commands "
                        "WHERE command_id=:command"
                    ),
                    {"command": command_id},
                )

            clean = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "downgrade",
                    "037_add_lab_worker_attempts",
                ],
                cwd=BACKEND_ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            assert clean.returncode == 0, f"{clean.stdout}\n{clean.stderr}"
            async with engine.connect() as connection:
                revision = (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                ).scalar_one()
            assert revision == "037_add_lab_worker_attempts"
        finally:
            if writer_transaction.is_active:
                await writer_transaction.rollback()
            await writer.close()
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await engine.dispose()
