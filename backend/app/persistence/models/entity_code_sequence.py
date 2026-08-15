"""ORM model: entity_code_sequences (F-ID, contador atómico de código Véktor).

Una fila por `(tenant_id, entity_type, prefix)`. El valor se entrega con
`UPDATE ... SET next_value = next_value + 1 ... RETURNING next_value` — una
sola sentencia atómica, sin `SELECT MAX` ni reintento ante colisión. Dejar
huecos ante rollback es aceptable (no es numeración contable); lo que no es
aceptable es reciclar un valor ya entregado, y esta sentencia nunca entrega
el mismo valor dos veces sin importar cuántas transacciones concurrentes lo
pidan a la vez.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EntityCodeSequence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "entity_code_sequences"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    prefix: Mapped[str] = mapped_column(String(10), nullable=False)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "entity_type", "prefix", name="uq_entity_code_sequences_tenant_type_prefix"
        ),
    )
