"""首个管理员提权脚本。"""
import pytest
from sqlalchemy import select

from app.models.user import User
from scripts.grant_admin import set_admin


async def _user(db, email: str, *, is_admin: bool = False) -> User:
    u = User(name=email.split("@")[0], email=email, is_admin=is_admin)
    db.add(u)
    await db.commit()
    return u


@pytest.mark.anyio
async def test_grant_promotes_and_is_idempotent(db_session):
    await _user(db_session, "a@test.com")

    first = await set_admin(db_session, "a@test.com", grant=True, dry_run=False)
    assert "granted" in first

    row = (await db_session.execute(
        select(User).where(User.email == "a@test.com")
    )).scalar_one()
    assert row.is_admin is True

    second = await set_admin(db_session, "a@test.com", grant=True, dry_run=False)
    assert "already" in second


@pytest.mark.anyio
async def test_dry_run_writes_nothing(db_session):
    await _user(db_session, "b@test.com")

    msg = await set_admin(db_session, "b@test.com", grant=True, dry_run=True)
    assert "dry-run" in msg

    row = (await db_session.execute(
        select(User).where(User.email == "b@test.com")
    )).scalar_one()
    assert row.is_admin is False


@pytest.mark.anyio
async def test_unknown_email_raises(db_session):
    with pytest.raises(LookupError):
        await set_admin(db_session, "nobody@test.com", grant=True, dry_run=False)


@pytest.mark.anyio
async def test_revoke_refuses_to_remove_the_last_admin(db_session):
    """脚本存在的意义就是消灭「必须手工 SQL 才能救回来」的状态，
    所以它自己绝不能把管理员数清零。"""
    await _user(db_session, "solo@test.com", is_admin=True)

    with pytest.raises(ValueError, match="last admin"):
        await set_admin(db_session, "solo@test.com", grant=False, dry_run=False)

    row = (await db_session.execute(
        select(User).where(User.email == "solo@test.com")
    )).scalar_one()
    assert row.is_admin is True


@pytest.mark.anyio
async def test_revoke_works_when_another_admin_remains(db_session):
    await _user(db_session, "one@test.com", is_admin=True)
    await _user(db_session, "two@test.com", is_admin=True)

    msg = await set_admin(db_session, "two@test.com", grant=False, dry_run=False)
    assert "revoked" in msg

    row = (await db_session.execute(
        select(User).where(User.email == "two@test.com")
    )).scalar_one()
    assert row.is_admin is False
