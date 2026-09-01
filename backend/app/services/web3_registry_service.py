"""Read-only verification for resident-scoped Agent Passport transactions."""

from __future__ import annotations

from dataclasses import dataclass
import re

from eth_utils import is_address, keccak

from app.config import settings
from app.http import get_client


_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class PassportVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedPassport:
    agent_id: str
    resident_key: str
    registry_address: str
    chain_id: int


def resident_key(resident_id: str) -> str:
    return f"0x{keccak(text=resident_id).hex()}"


def _selector(signature: str) -> str:
    return keccak(text=signature)[:4].hex()


def _word_uint(value: int) -> str:
    if value < 0 or value >= 2**256:
        raise PassportVerificationError("Agent ID is outside uint256 range")
    return value.to_bytes(32, "big").hex()


def _word_address(value: str) -> str:
    if not is_address(value):
        raise PassportVerificationError("Wallet address is invalid")
    return bytes.fromhex(value.removeprefix("0x").lower()).rjust(32, b"\0").hex()


async def _rpc(method: str, params: list[object]) -> object:
    rpc_url = settings.web3_rpc_url.strip()
    if not rpc_url:
        raise PassportVerificationError("Web3 RPC is not configured")
    response = await get_client().post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=12.0,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        message = payload["error"].get("message") if isinstance(payload["error"], dict) else None
        raise PassportVerificationError(message or f"RPC {method} failed")
    return payload.get("result")


async def _eth_call(registry: str, data: str) -> str:
    result = await _rpc("eth_call", [{"to": registry, "data": f"0x{data}"}, "latest"])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise PassportVerificationError("Contract returned an invalid response")
    return result


async def verify_passport_registration(
    *, wallet_address: str, resident_id: str, agent_id: str, transaction_hash: str | None,
    metadata_uri: str, metadata_hash: str,
) -> VerifiedPassport:
    registry = settings.web3_agent_registry_address.strip().lower()
    if not is_address(registry):
        raise PassportVerificationError("Agent Registry is not configured")
    try:
        numeric_agent_id = int(agent_id, 10)
    except ValueError as exc:
        raise PassportVerificationError("Agent ID must be a decimal uint256") from exc
    normalized_agent_id = str(numeric_agent_id)
    key = resident_key(resident_id)
    if not _HASH_RE.fullmatch(metadata_hash):
        raise PassportVerificationError("Passport metadata hash is invalid")
    if not metadata_uri.startswith(("https://", "ipfs://", "ar://")) or len(metadata_uri) > 1000:
        raise PassportVerificationError("Passport metadata URI is invalid")

    if transaction_hash is not None:
        if not _HASH_RE.fullmatch(transaction_hash):
            raise PassportVerificationError("Registration transaction hash is invalid")
        receipt = await _rpc("eth_getTransactionReceipt", [transaction_hash])
        if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
            raise PassportVerificationError("Registration transaction is not confirmed")
        transaction = await _rpc("eth_getTransactionByHash", [transaction_hash])
        if not isinstance(transaction, dict) or str(transaction.get("to", "")).lower() != registry:
            raise PassportVerificationError("Registration transaction targets another contract")
        if str(transaction.get("from", "")).lower() != wallet_address.lower():
            raise PassportVerificationError("Registration transaction was sent by another wallet")

    owner_raw = await _eth_call(
        registry, f"{_selector('ownerOf(uint256)')}{_word_uint(numeric_agent_id)}"
    )
    owner = f"0x{owner_raw[-40:]}".lower()
    if owner != wallet_address.lower():
        raise PassportVerificationError("Wallet does not own this Agent Passport")

    linked_raw = await _eth_call(
        registry,
        f"{_selector('agentByResident(address,bytes32)')}"
        f"{_word_address(wallet_address)}{key.removeprefix('0x')}",
    )
    linked_id = int(linked_raw, 16)
    if linked_id != numeric_agent_id:
        raise PassportVerificationError("Agent Passport is not linked to this resident")

    reverse_raw = await _eth_call(
        registry, f"{_selector('residentKeyOf(uint256)')}{_word_uint(numeric_agent_id)}"
    )
    if reverse_raw.lower() != key.lower():
        raise PassportVerificationError("Resident link does not match the Agent Passport")

    state_raw = await _eth_call(
        registry, f"{_selector('agentState(uint256)')}{_word_uint(numeric_agent_id)}"
    )
    if len(state_raw) < 66 or f"0x{state_raw[2:66]}".lower() != metadata_hash.lower():
        raise PassportVerificationError("Passport metadata hash does not match the chain")

    uri_raw = await _eth_call(
        registry, f"{_selector('tokenURI(uint256)')}{_word_uint(numeric_agent_id)}"
    )
    try:
        encoded = bytes.fromhex(uri_raw.removeprefix("0x"))
        offset = int.from_bytes(encoded[:32], "big")
        length = int.from_bytes(encoded[offset:offset + 32], "big")
        chain_uri = encoded[offset + 32:offset + 32 + length].decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise PassportVerificationError("Passport metadata URI is invalid") from exc
    if chain_uri != metadata_uri:
        raise PassportVerificationError("Passport metadata URI does not match the chain")

    return VerifiedPassport(
        agent_id=normalized_agent_id,
        resident_key=key.lower(),
        registry_address=registry,
        chain_id=settings.web3_chain_id,
    )
