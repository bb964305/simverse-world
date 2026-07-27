#!/usr/bin/env python3
"""按 email 提权 / 降权一个用户。**新环境的第一个管理员就靠它。**

在此之前全仓没有任何创建管理员的路径：``is_admin`` 只有模型默认值与迁移
server_default，唯一的写入口 ``PATCH /admin/users/{id}`` 自身就要求 admin 身份。
于是新环境部署完只能上生产手工跑 ``UPDATE users SET is_admin=true`` —— 与 07-25
事故同一类未受控的手工 SQL 操作面。

用法（vm212 api 容器内，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/grant_admin.py --email you@example.com          # dry-run
    docker compose exec api python scripts/grant_admin.py --email you@example.com --apply

降权::

    docker compose exec api python scripts/grant_admin.py --email x@example.com --revoke --apply

``--dry-run`` 是默认行为，必须显式 ``--apply`` 才写库。降权时拒绝清零管理员
（否则就会造出这个脚本本来要解决的那个死锁）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def set_admin(db, email: str, *, grant: bool, dry_run: bool) -> str:
    """Promote/demote by email. Returns a human-readable audit line.

    Raises LookupError if the email is unknown, ValueError if revoking would
    leave the deployment with zero admins.
    """
    from sqlalchemy import func, select

    from app.models.user import User

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise LookupError(f"no user with email {email!r}")

    if user.is_admin == grant:
        state = "already an admin" if grant else "already not an admin"
        return f"{email} ({user.id}) is {state} — nothing to do"

    if not grant:
        admin_count = (await db.execute(
            select(func.count(User.id)).where(
                User.is_admin.is_(True), User.is_banned.is_(False)
            )
        )).scalar() or 0
        if admin_count <= 1:
            raise ValueError(
                f"refusing to revoke the last admin ({email}); "
                "promote someone else first"
            )

    verb = "granted" if grant else "revoked"
    if dry_run:
        return f"[dry-run] would have {verb} admin on {email} ({user.id})"

    user.is_admin = grant
    await db.commit()
    return f"{verb} admin on {email} ({user.id})"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="target user's email address")
    parser.add_argument("--revoke", action="store_true", help="demote instead of promote")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it the script is a dry-run")
    args = parser.parse_args()

    from app.database import async_session

    async with async_session() as db:
        try:
            print(await set_admin(db, args.email, grant=not args.revoke,
                                  dry_run=not args.apply))
        except (LookupError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
