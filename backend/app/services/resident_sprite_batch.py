"""Crash-safe orchestration and static installation for the 25 sprite slots."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Literal

from PIL import Image
from pydantic import AwareDatetime, Field, StrictInt, field_validator, model_validator

from app.services.resident_sprite_artifacts import load_run, read_artifact
from app.services.resident_sprite_generation import (
    CapabilityContract,
    CapabilityReceipt,
    ResidentSpriteRequest,
    StrictContractModel,
    canonical_json_bytes,
    new_run_id,
    validate_non_symlink_path,
    validate_run_id,
)


CATALOG_SOURCE_POLICY = "first_party_text_spec_no_visual_reference"
EXPECTED_SLOT_COUNT = 25
TEXTURE_SIZE = (96, 128)
PORTRAIT_SIZE = (256, 256)
PHASER_SCREENSHOT_SIZE = (640, 360)
PHASER_REQUIRED_FRAMES = tuple(
    f"{direction}-walk.{index:03d}"
    for direction in ("down", "left", "right", "up")
    for index in range(3)
)
BATCH_FILE = "batch.json"
JOURNAL_FILE = ".resident-sprite-install.json"
LOCK_FILE = ".resident-sprite-install.lock"
GENERATED_FILES = frozenset({"texture.png", "portrait.png"})
ALLOWED_AGENT_SOURCE_FILES = frozenset({"agent.json", *GENERATED_FILES})


class ResidentSpriteBatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> ResidentSpriteBatchError:
    return ResidentSpriteBatchError(code, message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        raise _fail("FILE_INVALID", "required file is missing, unsafe, or too large") from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_bytes(path, max_bytes=1024 * 1024))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail("JSON_INVALID", "required JSON is invalid") from exc


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_non_symlink_path(path.parent, must_exist=True)
    if exclusive and path.exists():
        if _read_bytes(path) == data:
            return
        raise _fail("IMMUTABLE_CONFLICT", "immutable batch evidence already exists")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    _atomic_write(path, canonical_json_bytes(value), exclusive=exclusive)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class SpriteSlot(StrictContractModel):
    asset_key: str
    sprite_key: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    agent_json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    appearance: str = Field(min_length=1, max_length=1200)
    gender: Literal["male", "female", "neutral"]
    age_group: Literal["young", "adult", "elder"]
    vibe: str = Field(min_length=1, max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=8)
    direction_policy: Literal["mirror_right", "generate_right"] = "mirror_right"

    @model_validator(mode="after")
    def request_contract_valid(self) -> "SpriteSlot":
        ResidentSpriteRequest(
            **self.model_dump(exclude={"sprite_key", "agent_json_sha256"}),
            model="catalog-validation-model",
        )
        if self.display_name != self.sprite_key:
            raise ValueError("static slot display_name must equal sprite_key")
        return self


class SpriteSlotCatalog(StrictContractModel):
    schema_version: Literal[1] = 1
    catalog_id: str = Field(min_length=1, max_length=100)
    source_policy: Literal[CATALOG_SOURCE_POLICY]
    notes: list[str] = Field(default_factory=list, max_length=10)
    slots: list[SpriteSlot] = Field(min_length=EXPECTED_SLOT_COUNT, max_length=EXPECTED_SLOT_COUNT)

    @model_validator(mode="after")
    def unique_slots(self) -> "SpriteSlotCatalog":
        for field_name in ("asset_key", "sprite_key"):
            values = [getattr(slot, field_name) for slot in self.slots]
            if len(values) != len(set(values)):
                raise ValueError(f"catalog {field_name} values must be unique")
        return self


class BatchItem(StrictContractModel):
    asset_key: str
    sprite_key: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_file: str
    direction_policy: Literal["mirror_right", "generate_right"]
    run_id: str | None = None
    run_state: str = "pending"
    submitted_request_count: StrictInt = Field(default=0, ge=0, le=14)
    error_code: str | None = Field(default=None, max_length=100)
    source_batch_id: str | None = None
    superseded_run_ids: list[str] = Field(default_factory=list, max_length=8)
    superseded_request_count: StrictInt = Field(default=0, ge=0, le=14)

    @field_validator("run_id")
    @classmethod
    def valid_run_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_run_id(value)

    @field_validator("source_batch_id")
    @classmethod
    def valid_source_batch_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_run_id(value)

    @field_validator("superseded_run_ids")
    @classmethod
    def valid_superseded_run_ids(cls, values: list[str]) -> list[str]:
        validated = [validate_run_id(value) for value in values]
        if len(validated) != len(set(validated)):
            raise ValueError("superseded run IDs must be unique")
        return validated

    @field_validator("request_file")
    @classmethod
    def valid_request_file(cls, value: str) -> str:
        if value.startswith("/") or ".." in Path(value).parts or Path(value).parts[:1] != ("specs",):
            raise ValueError("request file must be a canonical specs path")
        return value


class PriceSnapshot(StrictContractModel):
    currency: Literal["USD"] = "USD"
    price_per_request_usd: str
    max_cost_usd: str
    cost_source: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_money(self) -> "PriceSnapshot":
        for value in (self.price_per_request_usd, self.max_cost_usd):
            try:
                parsed = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("price values must be decimal strings") from exc
            if not parsed.is_finite() or parsed <= 0 or parsed.as_tuple().exponent < -6:
                raise ValueError("price values must be positive with at most six decimals")
        return self


class SpriteBatch(StrictContractModel):
    schema_version: Literal[1] = 1
    batch_id: str
    catalog_id: str
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_policy: Literal[CATALOG_SOURCE_POLICY]
    model: str = Field(min_length=1, max_length=200)
    created_at: AwareDatetime
    baseline_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_requests_per_item: StrictInt
    max_requests_total: StrictInt
    price_snapshot: PriceSnapshot
    items: list[BatchItem] = Field(min_length=EXPECTED_SLOT_COUNT, max_length=EXPECTED_SLOT_COUNT)

    @field_validator("batch_id")
    @classmethod
    def valid_batch_id(cls, value: str) -> str:
        return validate_run_id(value)

    @model_validator(mode="after")
    def valid_totals(self) -> "SpriteBatch":
        if len({item.asset_key for item in self.items}) != EXPECTED_SLOT_COUNT:
            raise ValueError("batch items must have unique asset keys")
        if len({item.sprite_key for item in self.items}) != EXPECTED_SLOT_COUNT:
            raise ValueError("batch items must have unique sprite keys")
        expected_per_item = max(
            14 if item.direction_policy == "generate_right" else 11 for item in self.items
        )
        if self.max_requests_per_item != expected_per_item:
            raise ValueError("batch per-item request ceiling is inconsistent")
        expected_total = sum(
            14 if item.direction_policy == "generate_right" else 11 for item in self.items
        )
        if self.max_requests_total != expected_total:
            raise ValueError("batch total request ceiling is inconsistent")
        return self


def load_catalog(path: Path, agents_root: Path) -> tuple[SpriteSlotCatalog, str]:
    raw = _read_bytes(path, max_bytes=1024 * 1024)
    try:
        catalog = SpriteSlotCatalog.model_validate_json(raw)
    except Exception as exc:
        raise _fail("CATALOG_INVALID", "sprite slot catalog failed strict validation") from exc
    validate_agents_baseline(catalog, agents_root)
    return catalog, _sha256(canonical_json_bytes(catalog.model_dump(mode="json")))


def validate_agents_baseline(catalog: SpriteSlotCatalog, agents_root: Path) -> None:
    root = validate_non_symlink_path(agents_root, must_exist=True)
    if not root.is_dir():
        raise _fail("AGENT_ROOT_INVALID", "agent asset root is not a directory")
    expected = {slot.sprite_key for slot in catalog.slots}
    actual = {
        entry.name
        for entry in root.iterdir()
        if stat.S_ISDIR(entry.lstat().st_mode)
    }
    non_directories = {entry.name for entry in root.iterdir() if not entry.is_dir()}
    if actual != expected or non_directories not in (
        {"sprite.json"},
        {"sprite.json", "generation-batch.json"},
    ):
        raise _fail("AGENT_SET_DRIFT", "agent asset root does not match the canonical 25 slots")
    _read_json(root / "sprite.json")
    if "generation-batch.json" in non_directories:
        _read_json(root / "generation-batch.json")
    for slot in catalog.slots:
        directory = validate_non_symlink_path(root / slot.sprite_key, must_exist=True)
        entries = {entry.name for entry in directory.iterdir()}
        if entries not in (ALLOWED_AGENT_SOURCE_FILES, ALLOWED_AGENT_SOURCE_FILES | {"generation-provenance.json"}):
            raise _fail("AGENT_FILES_DRIFT", "agent directory contains unexpected files")
        for entry in directory.iterdir():
            if not stat.S_ISREG(entry.lstat().st_mode):
                raise _fail("AGENT_FILE_UNSAFE", "agent assets must be regular files")
        agent_bytes = _read_bytes(directory / "agent.json", max_bytes=1024 * 1024)
        if _sha256(agent_bytes) != slot.agent_json_sha256:
            raise _fail("AGENT_METADATA_DRIFT", "agent metadata hash differs from the catalog")
        agent = _read_json(directory / "agent.json")
        expected_portrait = f"assets/village/agents/{slot.sprite_key}/portrait.png"
        if not isinstance(agent, dict) or agent.get("name") != slot.sprite_key or agent.get("portrait") != expected_portrait:
            raise _fail("AGENT_METADATA_INVALID", "agent metadata identity or portrait path is inconsistent")
        _read_bytes(directory / "texture.png")
        _read_bytes(directory / "portrait.png")


def tree_sha256(root: Path) -> str:
    root = validate_non_symlink_path(root, must_exist=True)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise _fail("TREE_UNSAFE", "asset tree contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"D\0" + relative.encode("utf-8") + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F\0" + relative.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            raise _fail("TREE_UNSAFE", "asset tree contains a non-regular entry")
    return digest.hexdigest()


def _request_for(slot: SpriteSlot, model: str) -> ResidentSpriteRequest:
    return ResidentSpriteRequest(
        **slot.model_dump(exclude={"sprite_key", "agent_json_sha256"}),
        model=model,
    )


def prepare_batch(
    *,
    catalog_path: Path,
    agents_root: Path,
    batch_root: Path,
    model: str,
    price_per_request_usd: str,
    max_cost_usd: str,
    cost_source: str,
    batch_id: str | None = None,
) -> SpriteBatch:
    if model != model.strip() or not model:
        raise _fail("MODEL_INVALID", "model must be explicit canonical text")
    catalog, catalog_sha = load_catalog(catalog_path, agents_root)
    price = PriceSnapshot(
        price_per_request_usd=price_per_request_usd,
        max_cost_usd=max_cost_usd,
        cost_source=cost_source,
    )
    requests = [_request_for(slot, model) for slot in catalog.slots]
    total_ceiling = sum(11 if request.direction_policy == "mirror_right" else 14 for request in requests)
    worst_case = Decimal(price.price_per_request_usd) * total_ceiling
    if Decimal(price.max_cost_usd) < worst_case:
        raise _fail("COST_CAP_TOO_LOW", "max cost is below the worst-case request ceiling")
    selected_id = new_run_id() if batch_id is None else validate_run_id(batch_id)
    directory = batch_root / selected_id
    directory.mkdir(parents=True, exist_ok=True)
    validate_non_symlink_path(directory, must_exist=True)
    items: list[BatchItem] = []
    for slot, request in zip(catalog.slots, requests, strict=True):
        request_bytes = canonical_json_bytes(request.model_dump(mode="json"))
        request_file = f"specs/{slot.asset_key}.json"
        _atomic_write(directory / request_file, request_bytes, exclusive=True)
        items.append(
            BatchItem(
                asset_key=slot.asset_key,
                sprite_key=slot.sprite_key,
                request_sha256=_sha256(request_bytes),
                request_file=request_file,
                direction_policy=request.direction_policy,
            )
        )
    batch = SpriteBatch(
        batch_id=selected_id,
        catalog_id=catalog.catalog_id,
        catalog_sha256=catalog_sha,
        source_policy=catalog.source_policy,
        model=model,
        created_at=datetime.now(timezone.utc),
        baseline_tree_sha256=tree_sha256(agents_root),
        max_requests_per_item=max(11 if request.direction_policy == "mirror_right" else 14 for request in requests),
        max_requests_total=total_ceiling,
        price_snapshot=price,
        items=items,
    )
    _atomic_json(directory / BATCH_FILE, batch.model_dump(mode="json"), exclusive=True)
    return batch


def load_batch(batch_root: Path, batch_id: str) -> SpriteBatch:
    validate_run_id(batch_id)
    try:
        return SpriteBatch.model_validate_json(
            _read_bytes(batch_root / batch_id / BATCH_FILE, max_bytes=1024 * 1024)
        )
    except ResidentSpriteBatchError:
        raise
    except Exception as exc:
        raise _fail("BATCH_INVALID", "batch manifest failed strict validation") from exc


def _save_batch(batch_root: Path, batch: SpriteBatch) -> None:
    _atomic_json(batch_root / batch.batch_id / BATCH_FILE, batch.model_dump(mode="json"))


def reserve_run(batch_root: Path, batch_id: str, asset_key: str) -> tuple[SpriteBatch, BatchItem]:
    directory = batch_root / validate_run_id(batch_id)
    with _locked(directory / ".batch.lock"):
        batch = load_batch(batch_root, batch_id)
        item = next((candidate for candidate in batch.items if candidate.asset_key == asset_key), None)
        if item is None:
            raise _fail("BATCH_ITEM_UNKNOWN", "asset key is not part of the batch")
        if item.run_id is None:
            item = item.model_copy(update={"run_id": new_run_id(), "run_state": "reserved"})
            batch = batch.model_copy(
                update={"items": [item if old.asset_key == asset_key else old for old in batch.items]}
            )
            _save_batch(batch_root, batch)
        return batch, item


def sync_batch(batch_root: Path, batch_id: str, artifact_root: Path) -> SpriteBatch:
    directory = batch_root / validate_run_id(batch_id)
    with _locked(directory / ".batch.lock"):
        batch = load_batch(batch_root, batch_id)
        items: list[BatchItem] = []
        total = 0
        for item in batch.items:
            if item.run_id is None:
                items.append(item)
                continue
            try:
                run = load_run(artifact_root, item.run_id)
            except Exception:
                items.append(item.model_copy(update={"run_state": "reserved"}))
                continue
            request_bytes = canonical_json_bytes(run.request.model_dump(mode="json"))
            if _sha256(request_bytes) != item.request_sha256:
                raise _fail("RUN_REQUEST_MISMATCH", "run request differs from its frozen batch item")
            count = (
                item.superseded_request_count
                + run.request_budget.submitted_image_request_count
            )
            total += count
            if count > batch.max_requests_per_item or total > batch.max_requests_total:
                raise _fail("BATCH_BUDGET_EXCEEDED", "persisted run requests exceed the batch ceiling")
            items.append(
                item.model_copy(
                    update={
                        "run_state": run.state,
                        "submitted_request_count": count,
                        "error_code": None if run.error is None else run.error.code,
                    }
                )
            )
        batch = batch.model_copy(update={"items": items})
        _save_batch(batch_root, batch)
        return batch


def _capability_receipt_id(artifact_root: Path, run_id: str) -> str:
    try:
        payload = json.loads(read_artifact(artifact_root, run_id, "evidence/capability.json"))
        receipt = CapabilityReceipt.model_validate_json(
            canonical_json_bytes(payload["receipt"])
        )
    except Exception as exc:
        raise _fail(
            "CAPABILITY_EVIDENCE_INVALID", "run capability evidence is unavailable or invalid"
        ) from exc
    return receipt.receipt_id


def _copy_verified_run(source_root: Path, target_root: Path, run_id: str) -> None:
    source = validate_non_symlink_path(source_root / run_id, must_exist=True)
    target_root.mkdir(parents=True, exist_ok=True)
    validate_non_symlink_path(target_root, must_exist=True)
    target = target_root / run_id
    if target.exists():
        load_run(target_root, run_id)
        return
    for path in (source, *source.rglob("*")):
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise _fail("RUN_TREE_UNSAFE", "source run contains an unsafe filesystem entry")
    temporary_root = Path(tempfile.mkdtemp(prefix=".resident-run-import-", dir=target_root))
    temporary_run = temporary_root / run_id
    try:
        shutil.copytree(source, temporary_run)
        load_run(temporary_root, run_id)
        os.replace(temporary_run, target)
        _fsync_directory(target_root)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def consolidate_batch(
    *,
    batch_root: Path,
    batch_id: str,
    artifact_root: Path,
    source_batch_id: str,
    source_artifact_root: Path,
) -> SpriteBatch:
    """Carry forward matching immutable QC-passed runs without provider calls."""
    target = sync_batch(batch_root, batch_id, artifact_root)
    source = sync_batch(batch_root, source_batch_id, source_artifact_root)
    for field_name in (
        "catalog_id",
        "catalog_sha256",
        "source_policy",
        "model",
        "baseline_tree_sha256",
        "price_snapshot",
    ):
        if getattr(target, field_name) != getattr(source, field_name):
            raise _fail("CONSOLIDATION_BATCH_MISMATCH", f"batch field differs: {field_name}")

    target_capability_ids = {
        _capability_receipt_id(artifact_root, item.run_id)
        for item in target.items
        if item.run_id is not None
    }
    if len(target_capability_ids) != 1:
        raise _fail(
            "CONSOLIDATION_CAPABILITY_REQUIRED",
            "target runs must establish one capability receipt",
        )
    expected_capability_id = next(iter(target_capability_ids))
    source_items = {item.asset_key: item for item in source.items}
    directory = batch_root / validate_run_id(batch_id)
    with _locked(directory / ".batch.lock"):
        target = load_batch(batch_root, batch_id)
        updated_items: list[BatchItem] = []
        for item in target.items:
            if item.run_state == "auto_qc_passed":
                updated_items.append(item)
                continue
            candidate = source_items[item.asset_key]
            if (
                candidate.run_id is None
                or candidate.run_state != "auto_qc_passed"
                or candidate.request_sha256 != item.request_sha256
            ):
                raise _fail(
                    "CONSOLIDATION_SOURCE_UNAVAILABLE",
                    f"source has no matching QC-passed run: {item.asset_key}",
                )
            if (
                candidate.source_batch_id is not None
                or candidate.superseded_run_ids
                or candidate.superseded_request_count
            ):
                raise _fail(
                    "CONSOLIDATION_SOURCE_CHAIN_UNSUPPORTED",
                    "source runs must come directly from their original batch",
                )
            run = load_run(source_artifact_root, candidate.run_id)
            if _capability_receipt_id(source_artifact_root, candidate.run_id) != expected_capability_id:
                raise _fail(
                    "CONSOLIDATION_CAPABILITY_MISMATCH",
                    "source run uses a different capability receipt",
                )
            request_bytes = canonical_json_bytes(run.request.model_dump(mode="json"))
            if _sha256(request_bytes) != item.request_sha256:
                raise _fail("RUN_REQUEST_MISMATCH", "source run request differs from target item")

            _copy_verified_run(source_artifact_root, artifact_root, candidate.run_id)
            superseded_ids = list(item.superseded_run_ids)
            superseded_count = item.superseded_request_count
            if item.run_id is not None:
                current = load_run(artifact_root, item.run_id)
                superseded_ids.append(item.run_id)
                superseded_count += current.request_budget.submitted_image_request_count
            selected_count = run.request_budget.submitted_image_request_count
            if superseded_count + selected_count > (
                14 if item.direction_policy == "generate_right" else 11
            ):
                raise _fail(
                    "CONSOLIDATION_ITEM_BUDGET_EXCEEDED",
                    f"consolidated item exceeds its request ceiling: {item.asset_key}",
                )
            consolidated = item.model_copy(
                update={
                    "run_id": candidate.run_id,
                    "run_state": run.state,
                    "submitted_request_count": superseded_count + selected_count,
                    "error_code": None,
                    "source_batch_id": source_batch_id,
                    "superseded_run_ids": superseded_ids,
                    "superseded_request_count": superseded_count,
                }
            )
            evidence = {
                "schema_version": 1,
                "action": "qc_passed_run_carried_forward",
                "target_batch_id": batch_id,
                "source_batch_id": source_batch_id,
                "asset_key": item.asset_key,
                "request_sha256": item.request_sha256,
                "selected_run_id": candidate.run_id,
                "selected_request_count": selected_count,
                "superseded_run_ids": superseded_ids,
                "superseded_request_count": superseded_count,
                "capability_receipt_id": expected_capability_id,
            }
            _atomic_json(
                directory / "consolidations" / f"{item.asset_key}.json",
                evidence,
                exclusive=True,
            )
            updated_items.append(consolidated)
        target = SpriteBatch.model_validate(
            target.model_copy(update={"items": updated_items}).model_dump(mode="python")
        )
        if sum(item.submitted_request_count for item in target.items) > target.max_requests_total:
            raise _fail(
                "CONSOLIDATION_BATCH_BUDGET_EXCEEDED",
                "consolidated requests exceed the batch ceiling",
            )
        _save_batch(batch_root, target)
    return sync_batch(batch_root, batch_id, artifact_root)


def _png_info(data: bytes, expected_size: tuple[int, int]) -> dict[str, Any]:
    try:
        from io import BytesIO

        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.format != "PNG" or image.mode != "RGBA" or image.size != expected_size:
                raise ValueError
            width, height = image.size
    except Exception as exc:
        raise _fail("GENERATED_PNG_INVALID", "generated PNG has the wrong format, mode, or dimensions") from exc
    return {
        "sha256": _sha256(data),
        "bytes": len(data),
        "mime_type": "image/png",
        "width": width,
        "height": height,
        "color_mode": "RGBA",
    }


def _approved_run_evidence(
    artifact_root: Path,
    item: BatchItem,
    request: ResidentSpriteRequest,
    approved_reviewers: frozenset[str],
) -> tuple[bytes, bytes, dict[str, Any]]:
    if item.run_id is None:
        raise _fail("RUN_REQUIRED", "every batch item must have a run")
    run = load_run(artifact_root, item.run_id)
    if run.state != "human_approved":
        raise _fail("RUN_NOT_APPROVED", "every run must be human approved before installation")
    if run.request != request:
        raise _fail("RUN_REQUEST_MISMATCH", "approved run does not match its frozen request")
    if not run.provider_request_ids:
        raise _fail("PROVIDER_EVIDENCE_MISSING", "approved run has no provider request evidence")
    qc_bytes = read_artifact(artifact_root, item.run_id, "candidate/qc.json")
    phaser_bytes = read_artifact(artifact_root, item.run_id, "review/phaser.json")
    screenshot_bytes = read_artifact(
        artifact_root, item.run_id, "review/phaser-screenshot.png"
    )
    approval_bytes = read_artifact(artifact_root, item.run_id, "review/approval.json")
    capability_bytes = read_artifact(artifact_root, item.run_id, "evidence/capability.json")
    try:
        qc = json.loads(qc_bytes)
        phaser = json.loads(phaser_bytes)
        approval = json.loads(approval_bytes)
        capability_payload = json.loads(capability_bytes)
        capability = CapabilityReceipt.model_validate_json(
            canonical_json_bytes(capability_payload["receipt"])
        )
        contract = CapabilityContract.model_validate_json(
            canonical_json_bytes(capability_payload["contract"])
        )
    except Exception as exc:
        raise _fail("REVIEW_EVIDENCE_INVALID", "run review or capability evidence is invalid") from exc
    checks = approval.get("checks") if isinstance(approval, dict) else None
    if qc != {"direction_policy": request.direction_policy, "findings": [], "passed": True}:
        raise _fail("QC_EVIDENCE_INVALID", "run automatic QC evidence is incomplete")
    if not isinstance(checks, dict) or len(checks) != 9 or not all(value is True for value in checks.values()):
        raise _fail("APPROVAL_EVIDENCE_INVALID", "human approval checklist is incomplete")
    phaser_reviewer = phaser.get("reviewer")
    approval_reviewer = approval.get("reviewer")
    render = phaser.get("render") if isinstance(phaser, dict) else None
    try:
        _png_info(screenshot_bytes, PHASER_SCREENSHOT_SIZE)
    except ResidentSpriteBatchError as exc:
        raise _fail("PHASER_EVIDENCE_INVALID", "Phaser screenshot evidence is invalid") from exc
    if (
        phaser.get("schema_version") != 2
        or not isinstance(render, dict)
        or render.get("review_surface") != "phaser-canvas-v1"
        or render.get("phaser_version") != "3.90.0"
        or render.get("texture_sha256") != _sha256(
            read_artifact(artifact_root, item.run_id, "candidate/texture.png")
        )
        or tuple(render.get("frames", ())) != PHASER_REQUIRED_FRAMES
        or render.get("canvas_width") != PHASER_SCREENSHOT_SIZE[0]
        or render.get("canvas_height") != PHASER_SCREENSHOT_SIZE[1]
        or render.get("screenshot_sha256") != _sha256(screenshot_bytes)
    ):
        raise _fail("PHASER_EVIDENCE_INVALID", "Phaser render evidence is incomplete or mismatched")
    if (
        not approved_reviewers
        or phaser_reviewer not in approved_reviewers
        or approval_reviewer not in approved_reviewers
        or capability.operator in {phaser_reviewer, approval_reviewer}
    ):
        raise _fail("REVIEWER_NOT_ALLOWED", "review evidence does not satisfy the reviewer allowlist and separation rule")
    if contract != CapabilityContract.model_validate_json(canonical_json_bytes(capability.model_dump(exclude={
        "schema_version", "wire_receipt_id", "probe_id", "qualification_id", "operator",
        "reviewer", "qualified_at", "expires_at", "evidence_sha256", "provider_request_ids",
        "blind_scores", "latency_ms", "capability_request_count",
        "capability_cost_upper_bound_usd", "cost_source", "receipt_id",
    }))):
        raise _fail("CAPABILITY_EVIDENCE_INVALID", "capability receipt and contract differ")
    if request.model != capability.model_alias:
        raise _fail("CAPABILITY_MODEL_MISMATCH", "run model differs from its capability receipt")
    texture = read_artifact(artifact_root, item.run_id, "candidate/texture.png")
    portrait = read_artifact(artifact_root, item.run_id, "candidate/portrait.png")
    evidence = {
        "run_id": item.run_id,
        "request": request.model_dump(mode="json"),
        "request_sha256": item.request_sha256,
        "provider_request_ids": run.provider_request_ids,
        "submitted_request_count": run.request_budget.submitted_image_request_count,
        "capability_receipt_id": capability.receipt_id,
        "normalized_origin": capability.normalized_origin,
        "model_alias": capability.model_alias,
        "prompt_version": capability.prompt_version,
        "algorithm_version": capability.algorithm_version,
        "phaser_reviewer": phaser_reviewer,
        "approved_by": approval_reviewer,
        "review_surface": render["review_surface"],
        "phaser_version": render["phaser_version"],
        "phaser_frames": render["frames"],
        "phaser_screenshot_sha256": render["screenshot_sha256"],
        "phaser_evidence_sha256": _sha256(phaser_bytes),
        "approval_evidence_sha256": _sha256(approval_bytes),
        "capability_evidence_sha256": _sha256(capability_bytes),
    }
    return texture, portrait, evidence


def _receipt(
    *, batch: SpriteBatch, item: BatchItem, evidence: dict[str, Any], texture: bytes, portrait: bytes
) -> dict[str, Any]:
    texture_info = _png_info(texture, TEXTURE_SIZE)
    portrait_info = _png_info(portrait, PORTRAIT_SIZE)
    return {
        "schema_version": 1,
        "batch_id": batch.batch_id,
        "catalog_id": batch.catalog_id,
        "catalog_sha256": batch.catalog_sha256,
        "source_policy": batch.source_policy,
        "asset_key": item.asset_key,
        "sprite_key": item.sprite_key,
        "generation": {
            **evidence,
            "source_batch_id": item.source_batch_id,
            "superseded_run_ids": item.superseded_run_ids,
            "superseded_request_count": item.superseded_request_count,
            "estimated_cost_upper_bound_usd": str(
                Decimal(batch.price_snapshot.price_per_request_usd)
                * evidence["submitted_request_count"]
            ),
            "cost_source": batch.price_snapshot.cost_source,
        },
        "files": [
            {
                "file": f"agents/{item.sprite_key}/texture.png",
                "asset_kind": "resident_texture",
                **texture_info,
                "source_artifact": "candidate/texture.png",
                "audit_status": "cleared",
                "distribution_status": "allowed",
                "rights_basis": "first_party_generated",
            },
            {
                "file": f"agents/{item.sprite_key}/portrait.png",
                "asset_kind": "resident_portrait",
                **portrait_info,
                "source_artifact": "candidate/portrait.png",
                "derivation": {
                    "algorithm_version": evidence["request"]["algorithm_version"],
                    "source_file": f"agents/{item.sprite_key}/texture.png",
                    "source_sha256": texture_info["sha256"],
                    "frame": "down-walk.001",
                    "resize": "nearest-neighbor-256x256",
                },
                "audit_status": "cleared",
                "distribution_status": "allowed",
                "rights_basis": "first_party_generated",
            },
        ],
    }


def _build_staging_tree(
    *, batch: SpriteBatch, batch_root: Path, artifact_root: Path, agents_root: Path,
    staging: Path, approved_reviewers: frozenset[str], denied_sha256: frozenset[str],
) -> None:
    from app.services.resident_sprite_postprocess import derive_resident_portrait

    staging.mkdir(mode=0o700)
    _atomic_write(staging / "sprite.json", _read_bytes(agents_root / "sprite.json"))
    run_map: list[dict[str, str]] = []
    texture_hashes: set[str] = set()
    portrait_hashes: set[str] = set()
    for item in batch.items:
        request_path = batch_root / batch.batch_id / item.request_file
        request_bytes = _read_bytes(request_path, max_bytes=1024 * 1024)
        if _sha256(request_bytes) != item.request_sha256:
            raise _fail("BATCH_SPEC_DRIFT", "frozen batch request changed")
        request = ResidentSpriteRequest.model_validate_json(request_bytes)
        texture, portrait, evidence = _approved_run_evidence(
            artifact_root, item, request, approved_reviewers
        )
        texture_sha = _sha256(texture)
        portrait_sha = _sha256(portrait)
        if texture_sha in denied_sha256 or portrait_sha in denied_sha256:
            raise _fail("LEGACY_ASSET_REUSED", "generated candidate matches a denylisted legacy asset")
        if texture_sha in texture_hashes or portrait_sha in portrait_hashes:
            raise _fail("GENERATED_ASSET_DUPLICATE", "two sprite slots cannot install identical generated images")
        if derive_resident_portrait(texture) != portrait:
            raise _fail("PORTRAIT_DERIVATION_INVALID", "portrait is not derived from the generated down-idle frame")
        texture_hashes.add(texture_sha)
        portrait_hashes.add(portrait_sha)
        target = staging / item.sprite_key
        target.mkdir()
        _atomic_write(target / "agent.json", _read_bytes(agents_root / item.sprite_key / "agent.json"))
        _atomic_write(target / "texture.png", texture)
        _atomic_write(target / "portrait.png", portrait)
        receipt = _receipt(
            batch=batch, item=item, evidence=evidence, texture=texture, portrait=portrait
        )
        _atomic_json(target / "generation-provenance.json", receipt)
        run_map.append({"asset_key": item.asset_key, "sprite_key": item.sprite_key, "run_id": item.run_id or ""})
    _atomic_json(
        staging / "generation-batch.json",
        {
            "schema_version": 1,
            "batch_id": batch.batch_id,
            "catalog_id": batch.catalog_id,
            "catalog_sha256": batch.catalog_sha256,
            "source_policy": batch.source_policy,
            "model": batch.model,
            "max_requests_total": batch.max_requests_total,
            "price_snapshot": batch.price_snapshot.model_dump(mode="json"),
            "items": run_map,
        },
    )
    _fsync_directory(staging)


def install_batch(
    *, batch_root: Path, batch_id: str, artifact_root: Path, agents_root: Path,
    approved_reviewers: frozenset[str], denylist_path: Path,
) -> dict[str, str]:
    batch = sync_batch(batch_root, batch_id, artifact_root)
    if any(item.run_state != "human_approved" for item in batch.items):
        raise _fail("BATCH_NOT_APPROVED", "all 25 runs must be approved before installation")
    parent = validate_non_symlink_path(agents_root.parent, must_exist=True)
    staging = parent / f".agents-stage-{batch.batch_id}"
    backup = parent / f".agents-backup-{batch.batch_id}"
    journal = parent / JOURNAL_FILE
    denylist = _read_json(denylist_path)
    hashes = denylist.get("sha256") if isinstance(denylist, dict) else None
    if (
        not isinstance(denylist, dict)
        or denylist.get("schema_version") != 1
        or not isinstance(hashes, list)
        or len(hashes) != 50
        or len(set(hashes)) != 50
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        )
    ):
        raise _fail("LEGACY_DENYLIST_INVALID", "legacy sprite denylist is invalid")
    with _locked(parent / LOCK_FILE):
        if journal.exists():
            raise _fail("INSTALL_RECOVERY_REQUIRED", "an unfinished static install must be recovered first")
        if tree_sha256(agents_root) != batch.baseline_tree_sha256:
            raise _fail("INSTALL_BASELINE_DRIFT", "current agent tree differs from the frozen batch baseline")
        if staging.exists() or backup.exists():
            raise _fail("INSTALL_PATH_CONFLICT", "staging or backup path already exists")
        try:
            _build_staging_tree(
                batch=batch,
                batch_root=batch_root,
                artifact_root=artifact_root,
                agents_root=agents_root,
                staging=staging,
                approved_reviewers=approved_reviewers,
                denied_sha256=frozenset(hashes),
            )
            new_sha = tree_sha256(staging)
            journal_payload = {
                "schema_version": 1,
                "batch_id": batch.batch_id,
                "phase": "staged",
                "agents_root": agents_root.name,
                "staging": staging.name,
                "backup": backup.name,
                "old_tree_sha256": batch.baseline_tree_sha256,
                "new_tree_sha256": new_sha,
            }
            _atomic_json(journal, journal_payload, exclusive=True)
            os.replace(agents_root, backup)
            _fsync_directory(parent)
            journal_payload["phase"] = "old_moved"
            _atomic_json(journal, journal_payload)
            os.replace(staging, agents_root)
            _fsync_directory(parent)
            journal_payload["phase"] = "installed"
            _atomic_json(journal, journal_payload)
            if tree_sha256(agents_root) != new_sha or tree_sha256(backup) != batch.baseline_tree_sha256:
                raise _fail("INSTALL_VERIFY_FAILED", "installed or backup tree failed its hash check")
            shutil.rmtree(backup)
            _fsync_directory(parent)
            journal.unlink()
            _fsync_directory(parent)
            return {"batch_id": batch.batch_id, "state": "installed", "tree_sha256": new_sha}
        except Exception:
            # Preserve journal/staging/backup evidence for the explicit recovery command.
            if not journal.exists() and staging.exists() and agents_root.exists() and not backup.exists():
                shutil.rmtree(staging)
            raise


def recover_install(agents_root: Path, *, action: Literal["finish", "rollback"]) -> dict[str, str]:
    parent = validate_non_symlink_path(agents_root.parent, must_exist=True)
    journal = parent / JOURNAL_FILE
    with _locked(parent / LOCK_FILE):
        payload = _read_json(journal)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise _fail("INSTALL_JOURNAL_INVALID", "install journal is invalid")
        try:
            batch_id = validate_run_id(payload.get("batch_id"))
        except Exception as exc:
            raise _fail("INSTALL_JOURNAL_INVALID", "install journal batch ID is invalid") from exc
        expected_staging = f".agents-stage-{batch_id}"
        expected_backup = f".agents-backup-{batch_id}"
        if (
            payload.get("agents_root") != agents_root.name
            or payload.get("staging") != expected_staging
            or payload.get("backup") != expected_backup
        ):
            raise _fail("INSTALL_JOURNAL_INVALID", "install journal paths are not canonical")
        staging = parent / expected_staging
        backup = parent / expected_backup
        old_sha = payload.get("old_tree_sha256")
        new_sha = payload.get("new_tree_sha256")
        phase = payload.get("phase")
        if action == "finish":
            if phase == "staged":
                if not agents_root.exists() or tree_sha256(agents_root) != old_sha or tree_sha256(staging) != new_sha:
                    raise _fail("INSTALL_RECOVERY_CONFLICT", "staged recovery paths do not match the journal")
                os.replace(agents_root, backup)
                os.replace(staging, agents_root)
            elif phase == "old_moved":
                if agents_root.exists() or tree_sha256(backup) != old_sha or tree_sha256(staging) != new_sha:
                    raise _fail("INSTALL_RECOVERY_CONFLICT", "moved recovery paths do not match the journal")
                os.replace(staging, agents_root)
            elif phase == "installed":
                if tree_sha256(agents_root) != new_sha or tree_sha256(backup) != old_sha:
                    raise _fail("INSTALL_RECOVERY_CONFLICT", "installed recovery paths do not match the journal")
            else:
                raise _fail("INSTALL_JOURNAL_INVALID", "install journal has an unknown phase")
            if tree_sha256(agents_root) != new_sha:
                raise _fail("INSTALL_RECOVERY_CONFLICT", "finished tree does not match the journal")
            if backup.exists():
                shutil.rmtree(backup)
            if staging.exists():
                shutil.rmtree(staging)
            result = "installed"
        else:
            if phase == "staged":
                if tree_sha256(agents_root) != old_sha or tree_sha256(staging) != new_sha:
                    raise _fail("INSTALL_RECOVERY_CONFLICT", "staged rollback paths do not match the journal")
                shutil.rmtree(staging)
            elif phase == "old_moved":
                if agents_root.exists() or tree_sha256(backup) != old_sha or tree_sha256(staging) != new_sha:
                    raise _fail("INSTALL_RECOVERY_CONFLICT", "moved rollback paths do not match the journal")
                os.replace(backup, agents_root)
                shutil.rmtree(staging)
            elif phase == "installed":
                if tree_sha256(agents_root) != new_sha or tree_sha256(backup) != old_sha:
                    raise _fail("INSTALL_RECOVERY_CONFLICT", "installed rollback paths do not match the journal")
                discarded = parent / f".agents-discard-{batch_id}"
                if discarded.exists():
                    raise _fail("INSTALL_PATH_CONFLICT", "rollback discard path already exists")
                os.replace(agents_root, discarded)
                os.replace(backup, agents_root)
                shutil.rmtree(discarded)
            else:
                raise _fail("INSTALL_JOURNAL_INVALID", "install journal has an unknown phase")
            if tree_sha256(agents_root) != old_sha:
                raise _fail("INSTALL_RECOVERY_CONFLICT", "rolled-back tree does not match the journal")
            result = "rolled_back"
        journal.unlink()
        _fsync_directory(parent)
        return {"batch_id": batch_id, "state": result}
