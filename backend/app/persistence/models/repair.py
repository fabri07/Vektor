"""ORM models for data repair audit: data_repair_runs, data_repair_items."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, UUIDPrimaryKeyMixin


class DataRepairRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "data_repair_runs"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    repair_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    dry_run: Mapped[bool] = mapped_column(nullable=False, default=True)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_repair_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    candidates_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sales_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sales_voided: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    # F-RR (revisión de relectura): usada por el sweep de sesiones/jobs
    # huérfanos para saber cuánto hace que un run no avanzó — `created_at` no
    # alcanza porque una sesión de revisión (PREVIEWING/NEEDS_REVIEW/
    # READY_TO_APPLY) puede estar activa (el usuario corrigiendo mapeos) mucho
    # más que el umbral de "colgado" sin ser huérfana.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    # F-RR (2026-08-26): momento en que el run entró EN COLA para aplicarse.
    # SEPARADO de `updated_at` a propósito, mismo motivo que
    # `uploaded_files.import_started_at` (mig 20260801_0001): `updated_at` se
    # mueve con CUALQUIER escritura — y el reclamo QUEUED->APPLYING del worker
    # lo pisa — así que servir el cronómetro "empezado hace..." desde ahí lo
    # hacía retroceder justo cuando el worker tomaba el run. Se escribe una
    # sola vez, al entrar a QUEUED, y nunca más. Nullable sin backfill: los
    # runs que ya estaban en vuelo al deployar no lo tienen (ver el fallback
    # explícito en `api/v1/ingestion.py`).
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED',"
            "'REVERTED','PARTIALLY_APPLIED','COMPLETED_WITH_ERRORS',"
            # F-RR: estados de la sesión de revisión de relectura (ver
            # reread_service.py) — PREVIEWING/NEEDS_REVIEW/READY_TO_APPLY
            # cubren la revisión de mapeo antes de aplicar; QUEUED/APPLYING
            # reemplazan la granularidad plana que antes vivía toda en
            # RUNNING. APPLIED/FAILED ya existían y se reusan tal cual.
            "'PREVIEWING','NEEDS_REVIEW','READY_TO_APPLY','QUEUED','APPLYING')",
            name="ck_repair_runs_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DataRepairRun id={self.id} type={self.repair_type!r} "
            f"dry_run={self.dry_run} status={self.status!r}>"
        )


class DataRepairItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "data_repair_items"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_repair_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sale_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE',"
            "'REVIEW_SALE','VOID_DUPLICATE','RECLASSIFY_EXPENSE','REREAD_VOID','REREAD_INSERT',"
            "'MERGE_PRODUCT','DEACTIVATE_DUPLICATE','REPOINT_FK','CONSOLIDATE_BALANCE',"
            "'DELETE_BALANCE','REREAD_MASTER_CREATE','REREAD_MASTER_UPDATE',"
            # Maestros creados/modificados por un IMPORT (mig 20260810_0001). Los
            # `REREAD_MASTER_*` de arriba son el camino de la relectura y quedan.
            "'CREATE_CUSTOMER','UPDATE_CUSTOMER','CREATE_SUPPLIER','UPDATE_SUPPLIER')",
            name="ck_repair_items_action",
        ),
    )

    def __repr__(self) -> str:
        return f"<DataRepairItem run={self.run_id} action={self.action!r} tenant={self.tenant_id}>"
