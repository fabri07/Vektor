"""ORM de los alias de cliente: customer_aliases.

Un alias es otro nombre con el que un archivo nombra a un cliente —«Caro G.»,
«carolina g», «Caro Gimenez» para Carolina Giménez—, aprendido cuando una persona
resuelve desde «Otros» una venta fiada o un cobro eligiendo el cliente. El importador
lo consulta antes de dejar un fiado sin cliente en «Otros».

Mismo contrato que ``ProductAlias`` (B·8a): sin FK al archivo ni a la fila de
«Otros» —lo aprendió una persona, no un archivo, así que borrar el archivo no lo
borra—, y único por ``(tenant_id, alias_normalizado)``: un alias identifica UN
cliente; si fuera de dos, no identificaría a nadie.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, UUIDPrimaryKeyMixin

#: El único origen hoy: lo eligió una persona al resolver una fila de «Otros».
CUSTOMER_ALIAS_ORIGIN_MANUAL = "manual"


class CustomerAlias(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "customer_aliases"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Tal como lo escribió el archivo (para mostrarlo).
    alias: Mapped[str] = mapped_column(String(300), nullable=False)
    #: `normalize_text(alias)`: la clave por la que se busca.
    alias_normalizado: Mapped[str] = mapped_column(String(300), nullable=False)
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CUSTOMER_ALIAS_ORIGIN_MANUAL
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "alias_normalizado", name="uq_customer_aliases_tenant_alias"
        ),
        Index("ix_customer_aliases_tenant_customer", "tenant_id", "customer_id"),
        CheckConstraint("origin IN ('manual')", name="ck_customer_aliases_origin"),
    )
