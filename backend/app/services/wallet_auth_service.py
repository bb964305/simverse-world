"""Wallet authentication using a short-lived EIP-4361-style challenge.

The wallet signature proves control of an EVM externally owned account (EOA).
After verification the application still uses its existing JWT sessions, so
REST authorization and WebSocket authentication remain unchanged.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.transaction import Transaction
from app.models.user import User
from app.redis_client import get_redis
from app.services.auth_service import create_token

logger = logging.getLogger(__name__)

CHALLENGE_TTL_SECONDS = 300
_CHALLENGE_PREFIX = "wallet_challenge:"
_memory_challenges: dict[str, tuple[float, str]] = {}


class WalletAuthError(ValueError):
    """A safe, user-facing wallet authentication failure."""


def normalize_address(address: str) -> str:
    if not is_address(address):
        raise WalletAuthError("钱包地址格式无效")
    return to_checksum_address(address)


def trusted_origin(request_origin: str | None) -> str:
    """Select a configured frontend origin and reject arbitrary domains."""
    allowed = {origin.rstrip("/") for origin in settings.cors_origins}
    configured = settings.web3_uri.rstrip("/")
    if configured:
        allowed.add(configured)
    if request_origin:
        candidate = request_origin.rstrip("/")
        if candidate not in allowed:
            raise WalletAuthError("钱包签名来源不在允许列表中")
        return candidate
    if configured:
        return configured
    if allowed:
        return sorted(allowed)[0]
    raise WalletAuthError("WEB3_URI 或 CORS_ORIGINS 尚未配置")


def _message(
    *,
    address: str,
    origin: str,
    chain_id: int,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WalletAuthError("钱包签名 URI 配置无效")
    return (
        f"{parsed.netloc} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        "Authenticate your Simverse identity. This request will not trigger a blockchain transaction.\n\n"
        f"URI: {origin}\n"
        "Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at.isoformat()}\n"
        f"Expiration Time: {expires_at.isoformat()}\n"
        "Resources:\n"
        "- urn:simverse:session"
    )


async def _store_challenge(nonce: str, payload: str) -> None:
    try:
        stored = await get_redis().set(
            f"{_CHALLENGE_PREFIX}{nonce}", payload, ex=CHALLENGE_TTL_SECONDS, nx=True
        )
        if not stored:
            raise WalletAuthError("签名挑战生成冲突，请重试")
        return
    except WalletAuthError:
        raise
    except Exception as exc:  # local-dev resilience when Redis is unavailable
        logger.warning("Wallet challenge Redis unavailable: %s; using process memory", exc)
    expires = datetime.now(UTC).timestamp() + CHALLENGE_TTL_SECONDS
    _memory_challenges[nonce] = (expires, payload)


async def _take_challenge(nonce: str) -> str | None:
    try:
        payload = await get_redis().getdel(f"{_CHALLENGE_PREFIX}{nonce}")
        if payload is not None:
            return str(payload)
    except Exception as exc:  # local-dev resilience when Redis is unavailable
        logger.warning("Wallet challenge Redis unavailable: %s; checking process memory", exc)
    stored = _memory_challenges.pop(nonce, None)
    if stored is None:
        return None
    expires, payload = stored
    return payload if expires > datetime.now(UTC).timestamp() else None


async def create_wallet_challenge(
    *, address: str, chain_id: int, origin: str
) -> dict[str, str | int]:
    if not settings.web3_enabled:
        raise WalletAuthError("Web3 登录当前未开放")
    if chain_id != settings.web3_chain_id:
        raise WalletAuthError(f"请切换到 {settings.web3_chain_name}")

    checksum_address = normalize_address(address)
    nonce = secrets.token_hex(16)
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=CHALLENGE_TTL_SECONDS)
    message = _message(
        address=checksum_address,
        origin=origin,
        chain_id=chain_id,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    payload = json.dumps(
        {
            "address": checksum_address.lower(),
            "chain_id": chain_id,
            "message": message,
            "expires_at": expires_at.isoformat(),
        },
        separators=(",", ":"),
    )
    await _store_challenge(nonce, payload)
    return {
        "message": message,
        "nonce": nonce,
        "expires_at": expires_at.isoformat(),
        "chain_id": chain_id,
        "chain_name": settings.web3_chain_name,
    }


async def _find_or_create_wallet_user(
    db: AsyncSession, checksum_address: str
) -> User:
    stored_address = checksum_address.lower()
    result = await db.execute(select(User).where(User.wallet_address == stored_address))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        name=f"Soul {checksum_address[:6]}…{checksum_address[-4:]}",
        email=f"wallet-{stored_address[2:]}@identity.simverse.world",
        hashed_password=None,
        wallet_address=stored_address,
    )
    db.add(user)
    try:
        await db.flush()
        db.add(Transaction(user_id=user_id, amount=100, reason="wallet_signup_bonus"))
        await db.commit()
        await db.refresh(user)
        return user
    except IntegrityError:
        # Two valid signatures can race on the same address. Preserve the one
        # durable identity and resume it instead of surfacing a random 500.
        await db.rollback()
        result = await db.execute(select(User).where(User.wallet_address == stored_address))
        existing = result.scalar_one_or_none()
        if existing is None:
            raise WalletAuthError("钱包身份创建失败，请重试")
        return existing


async def verify_wallet_signature(
    db: AsyncSession,
    *,
    address: str,
    message: str,
    signature: str,
    nonce: str,
    chain_id: int,
) -> tuple[User, str]:
    if not settings.web3_enabled:
        raise WalletAuthError("Web3 登录当前未开放")
    checksum_address = normalize_address(address)
    raw = await _take_challenge(nonce)
    if raw is None:
        raise WalletAuthError("签名挑战已过期或已使用，请重新连接")

    try:
        challenge = json.loads(raw)
        expires_at = datetime.fromisoformat(challenge["expires_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WalletAuthError("签名挑战数据无效，请重新连接") from exc
    if expires_at <= datetime.now(UTC):
        raise WalletAuthError("签名挑战已过期，请重新连接")
    if chain_id != settings.web3_chain_id or challenge.get("chain_id") != chain_id:
        raise WalletAuthError(f"请切换到 {settings.web3_chain_name}")
    if challenge.get("address") != checksum_address.lower() or challenge.get("message") != message:
        raise WalletAuthError("签名内容与挑战不匹配")

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:
        raise WalletAuthError("钱包签名格式无效") from exc
    if recovered.lower() != checksum_address.lower():
        raise WalletAuthError("签名地址验证失败")

    user = await _find_or_create_wallet_user(db, checksum_address)
    return user, create_token(user.id)


def _reset_wallet_challenges_for_tests() -> None:
    _memory_challenges.clear()
