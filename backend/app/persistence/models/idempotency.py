"""Registro de peticiones idempotentes con su RESULTADO.

Por qué no alcanza `operation_fingerprints`
-------------------------------------------
El mecanismo anterior (`claim_idempotency_key`) sólo responde una pregunta:
"¿esta clave ya se usó?". Devuelve un bool y el caller contesta 409 vacío. Eso
evita el duplicado, que es la mitad del problema.

La otra mitad es la que aparece con una caja: **el servidor guardó la venta y el
cliente nunca recibió la respuesta** (corte justo después del commit, timeout del
proxy, el navegador que se cerró). El reintento tiene que poder recuperar ESE
ticket —con sus líneas y sus pagos— para imprimirlo y para sacarlo de la cola. Un
409 sin cuerpo deja al cajero sin saber si cobró.

Y una tercera: la misma clave con contenido DISTINTO no es un reintento, es un
error del cliente (una clave reusada por accidente). Sin guardar de qué era la
clave, esa reutilización se traga en silencio y la segunda venta desaparece.

Contrato
--------
* ``UNIQUE (tenant_id, key)`` es el candado; la carrera la resuelve el índice,
  no un SELECT previo.
* ``request_hash`` es sobre el contenido COMERCIAL ya validado, no sobre los
  bytes crudos: el orden de las claves del JSON o un espacio de más no pueden
  producir un conflicto falso.
* ``response_json`` se completa al final de la MISMA transacción que la entidad.
  Si queda en NULL (una ruta que reclamó y no registró) el replay degrada al
  comportamiento viejo — un 409 — en vez de inventar una respuesta.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.persistence.db.base import Base

PGJSONB = JSONB().with_variant(JSON(), "sqlite")


class IdempotencyRecord(Base):
    """Una petición idempotente reclamada, con el resultado que produjo."""

    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # NULL = se reclamó la clave pero no se registró el resultado. El replay lo
    # trata como "no puedo devolverte el original" y contesta 409, nunca inventa.
    response_json: Mapped[dict[str, object] | None] = mapped_column(PGJSONB, nullable=True)
    http_status: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=201)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "key", name="uq_idempotency_records_tenant_key"),
        sa.Index("ix_idempotency_records_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug
        return f"<IdempotencyRecord tenant={self.tenant_id} key={self.key}>"
