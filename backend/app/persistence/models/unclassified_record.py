"""ORM model: unclassified_records — bandeja de revisión "Otros".

Todo lo que llega a Véktor por chat o ingesta y NO se clasifica como venta,
gasto o producto queda acá en vez de descartarse en silencio (o, peor, caer
al bucket de ventas por default). El tenant lo revisa en /otros y lo importa
como venta/gasto/producto o lo descarta.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

UNCLASSIFIED_STATUS_PENDING = "PENDING"
UNCLASSIFIED_STATUS_IMPORTED = "IMPORTED"
UNCLASSIFIED_STATUS_DISMISSED = "DISMISSED"

UNCLASSIFIED_SOURCES = ("ingestion", "chat", "reanalysis")


class UnclassifiedRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "unclassified_records"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 'ingestion' | 'chat' | 'reanalysis'
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # Hoja/grupo de origen ("Hoja 3", "general") o motivo ("monto no parseable").
    context_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Headers de la fila original (orden del archivo) y datos crudos.
    headers: Mapped[list[str] | None] = mapped_column(PGJSONB, nullable=True)
    row_data: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False)
    # Sugerencia del clasificador (si la hubo): 'sale' | 'expense' | 'product'.
    suggested_entity: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(
        String(15), nullable=False, default=UNCLASSIFIED_STATUS_PENDING
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'IMPORTED', 'DISMISSED')",
            name="ck_unclassified_records_status",
        ),
        CheckConstraint(
            "source IN ('ingestion', 'chat', 'reanalysis')",
            name="ck_unclassified_records_source",
        ),
        Index("ix_unclassified_records_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<UnclassifiedRecord tenant={self.tenant_id}"
            f" source={self.source!r} status={self.status!r}>"
        )
