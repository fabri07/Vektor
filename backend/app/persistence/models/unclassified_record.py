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

#: Prefijo del ``source_row_ref`` que se estampa en la venta/gasto nacido de
#: clasificar a mano una fila de "Otros" (``unclassified:{record_id}``).
#:
#: Es una IDENTIDAD, no un formato: distingue un registro cuya procedencia es una
#: decisión humana sobre una fila que el parser no supo leer, de uno que el
#: importador derivó del archivo. Vivía escrita a mano en tres lugares —el que la
#: estampa, el borrado por procedencia y ahora la relectura—, que es una copia de
#: más para algo cuyo desacuerdo no da error sino comportamiento distinto.
UNCLASSIFIED_ROW_REF_PREFIX = "unclassified:"


def unclassified_row_ref(record_id: uuid.UUID) -> str:
    """El ``source_row_ref`` de un registro nacido de clasificar esta fila.

    Se deriva del id del ``UnclassifiedRecord`` y no de sus valores: es estable
    aunque el usuario corrija un campo antes de clasificar.
    """
    return f"{UNCLASSIFIED_ROW_REF_PREFIX}{record_id}"


def is_unclassified_row_ref(ref: str | None) -> bool:
    """¿Este registro nació de una clasificación manual desde "Otros"?"""
    return bool(ref) and str(ref).startswith(UNCLASSIFIED_ROW_REF_PREFIX)

#: Procedencias que los callers nombran con otra palabra. La relectura se
#: identifica a sí misma como ``"reread"`` (``insert_confirmed_data(...,
#: source="reread")``) y ese valor NO está en la CHECK: cualquier fila capturada
#: durante una relectura reventaba con `ck_unclassified_records_source` y se
#: llevaba puesta la transacción entera del apply. Una relectura ES un
#: reanálisis del mismo archivo, así que se guarda como tal.
_SOURCE_ALIASES = {"reread": "reanalysis"}


def normalize_unclassified_source(source: str) -> str:
    """Procedencia válida para la columna, resolviendo alias conocidos.

    Un valor desconocido cae a ``"ingestion"`` en vez de propagar el
    `IntegrityError`: la fila capturada es el ÚNICO rastro de un dato que no se
    pudo importar, y perderla —además de abortar una operación que ya escribió—
    es peor que guardarla con una procedencia genérica. El caller que mande algo
    fuera del set queda registrado en el log.
    """
    if source in UNCLASSIFIED_SOURCES:
        return source
    alias = _SOURCE_ALIASES.get(source)
    if alias is not None:
        return alias
    return "ingestion"


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
    # Fase 2 (F2-T1) — candidatos de match para filas ambiguas (lo llena T2).
    # Forma: [{id, matched_by, name, sku, barcode}].
    match_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(
        PGJSONB, nullable=True
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
