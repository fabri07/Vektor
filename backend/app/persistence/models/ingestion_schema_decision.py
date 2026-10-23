"""ORM model: ingestion_schema_decisions (Bloque 5).

Decisiones EXPLÍCITAS del usuario sobre cómo interpretar un esquema de
archivo — nunca sugerencias automáticas. Clave (tenant_id, schema_fingerprint,
context_signature, decision_type): una fila por decisión, no una por hoja —
así una relectura puede pisar solo `stock_treatment` sin tocar `column_mapping`.
"""

import uuid
from typing import Any, Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Set cerrado — agregar un tipo nuevo obliga a tocar esta lista + el CHECK de
#: la migración (mismo criterio que ActionType en shared/schemas.py).
DecisionType = Literal[
    "column_mapping",
    "context_entity",
    "context_included",
    "stock_treatment",
    "shipping_decision",
]
DECISION_TYPES: tuple[str, ...] = (
    "column_mapping",
    "context_entity",
    "context_included",
    "stock_treatment",
    "shipping_decision",
)

#: Versión del FORMATO del payload — no del dato. Subir esto invalida (para
#: lectura) las filas viejas sin migración: `lookup_context_decisions` solo
#: devuelve filas con `format_version == CURRENT_DECISION_FORMAT_VERSION`.
CURRENT_DECISION_FORMAT_VERSION = 1


class IngestionSchemaDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ingestion_schema_decisions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False)
    format_version: Mapped[int] = mapped_column(
        nullable=False, default=CURRENT_DECISION_FORMAT_VERSION
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "schema_fingerprint",
            "context_signature",
            "decision_type",
            name="uq_ingestion_schema_decisions_tenant_schema_context_type",
        ),
        CheckConstraint(
            "decision_type IN ("
            "'column_mapping','context_entity','context_included',"
            "'stock_treatment','shipping_decision')",
            name="ck_ingestion_schema_decisions_type",
        ),
        Index(
            "ix_ingestion_schema_decisions_lookup",
            "tenant_id",
            "schema_fingerprint",
            "context_signature",
        ),
    )
