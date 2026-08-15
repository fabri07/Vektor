"""ORM model: entity_identifiers (F-ID, capa 3 — identificadores externos).

Una entidad (producto/cliente/proveedor) puede acumular VARIOS identificadores
de fuentes distintas a lo largo del tiempo — el SKU real del negocio, un
código de un sistema anterior, el código Véktor que generamos — sin que
ninguno se pierda y sin que dos fuentes que reusan el mismo valor crudo
colisionen entre sí (``namespace`` las separa). Insert-only por diseño: una
fila NUNCA se borra, sólo se marca ``revoked_at`` cuando deja de ser la
primaria (p. ej. al fusionar dos entidades) — es la mitad estructural del
no-reciclo de código Véktor (la otra mitad es la secuencia atómica en
``entity_code_sequences``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, UUIDPrimaryKeyMixin


class EntityIdentifier(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_identifiers"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'product' | 'customer' | 'supplier'. No es FK polimórfica — entity_id
    # apunta a la tabla que indique entity_type, resuelto en el código.
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 'vektor_code' | 'sku' | 'barcode' | 'dni' | 'cuit' | 'email' | 'phone'
    # | 'business_code' | 'alias'.
    identifier_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # 'vektor' | 'business' | 'supplier:<supplier_id>' — separa fuentes que
    # reusan el mismo valor crudo sin que eso sea una colisión real.
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_value: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(300), nullable=False)
    # 'business' | 'vektor' | 'import' | 'user_confirmed'.
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    # NUNCA se borra la fila al revocar — insert-only, ver docstring del módulo.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_entity_identifiers_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
        ),
        Index(
            "ix_entity_identifiers_lookup",
            "tenant_id",
            "entity_type",
            "identifier_type",
            "namespace",
            "normalized_value",
        ),
        # Un valor normalizado no puede significar DOS entidades distintas
        # dentro del mismo (tenant, entity_type, identifier_type, namespace) —
        # pero sólo mientras esté VIGENTE (`revoked_at IS NULL`). Revocar libera
        # el valor para que una fusión lo reasigne al sobreviviente sin chocar
        # contra la fila vieja del perdedor, que se conserva como historia.
        Index(
            "uq_entity_identifiers_active_value",
            "tenant_id",
            "entity_type",
            "identifier_type",
            "namespace",
            "normalized_value",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EntityIdentifier {self.entity_type}:{self.entity_id} "
            f"{self.identifier_type}={self.raw_value!r} ns={self.namespace!r}>"
        )
