import uuid
from datetime import datetime, UTC

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabArtifact(Base):
    """A deliverable produced by a run (spec §4.3).

    Metadata is visible under task ACL. Content remains behind the download
    route until the task and exact scanned object version are both released.
    """

    __tablename__ = "lab_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "provider_artifact_id",
            name="uq_lab_artifacts_run_provider_artifact",
        ),
        CheckConstraint(
            "storage_status IN ('legacy','pending_upload','quarantined',"
            "'released','delete_pending','deleted')",
            name="ck_lab_artifacts_storage_status",
        ),
        CheckConstraint(
            "scan_status IN ('skipped','pending','scanning','clean','flagged','failed')",
            name="ck_lab_artifacts_scan_status",
        ),
        CheckConstraint(
            "verification_status IN ('unverified','verified','rejected')",
            name="ck_lab_artifacts_verification_status",
        ),
        CheckConstraint("byte_size >= 0", name="ck_lab_artifacts_byte_size"),
        CheckConstraint(
            "declared_byte_size IS NULL OR declared_byte_size >= 0",
            name="ck_lab_artifacts_declared_byte_size",
        ),
        CheckConstraint("scan_attempts >= 0", name="ck_lab_artifacts_scan_attempts"),
        CheckConstraint("producer_epoch >= 0", name="ck_lab_artifacts_producer_epoch"),
        CheckConstraint("row_version >= 1", name="ck_lab_artifacts_row_version"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="text")  # file|link|text|image|dataset
    title: Mapped[str] = mapped_column(String(200), default="")
    uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # storage path or external link
    text_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    # Integrity and retention fields remain nullable/defaulted so historical
    # DB-backed rows keep their explicit legacy behavior.
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Actual object-byte digest for production rows; legacy rows hash their
    # historical DB-backed content.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    producer_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provenance: Mapped[str] = mapped_column(String(30), default="runtime")  # runtime|verifier|system
    scan_status: Mapped[str] = mapped_column(String(20), default="skipped")  # skipped|pending|clean|flagged
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified")  # unverified|verified|rejected
    retention_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Production object lifecycle. Legacy DB-backed rows retain storage_status
    # "legacy" and continue to use text_md/uri while the production-v2 path is
    # disabled.
    provider_artifact_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    runtime_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    producer_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    declared_content_type: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    declared_byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_backend: Mapped[str | None] = mapped_column(String(30), nullable=True)
    storage_status: Mapped[str] = mapped_column(String(24), default="legacy", index=True)
    quarantine_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quarantine_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    quarantine_version_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quarantine_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    released_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    released_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    released_version_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    released_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scan_policy_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scan_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    scan_attempts: Mapped[int] = mapped_column(Integer, default=0)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_engine_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scan_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    upload_receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": row_version}


class LabArtifactOperation(Base):
    """Durable cross-service upload, scan, and delete operation."""

    __tablename__ = "lab_artifact_operations"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_lab_artifact_operations_operation"),
        CheckConstraint(
            "operation_type IN ('upload','scan','delete')",
            name="ck_lab_artifact_operations_type",
        ),
        CheckConstraint(
            "state IN ('pending','processing','succeeded','failed','quarantined')",
            name="ck_lab_artifact_operations_state",
        ),
        CheckConstraint("epoch >= 0", name="ck_lab_artifact_operations_epoch"),
        CheckConstraint("attempt >= 0", name="ck_lab_artifact_operations_attempt"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    operation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("lab_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    command_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    service_endpoint: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accounted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class LabArtifactHold(Base):
    """Auditable, releasable retention reason for an artifact."""

    __tablename__ = "lab_artifact_holds"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('task','world_proposal','manual','legal')",
            name="ck_lab_artifact_holds_source_type",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    artifact_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("lab_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
