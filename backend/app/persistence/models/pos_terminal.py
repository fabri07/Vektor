"""ORM de las terminales de caja (B6): pos_terminals.

Una fila = una PC habilitada por el dueño. Se guarda el sha256 del secreto,
nunca el secreto. Dar de baja es poner ``disabled_at``: la fila queda para que
las ventas viejas sigan diciendo desde qué caja se cobraron.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PosTerminal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pos_terminals"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    credential_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # Dos cajas activas con el mismo nombre no se pueden distinguir en la
        # lista. Una dada de baja libera el nombre.
        Index(
            "uq_pos_terminals_tenant_name_activa",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("disabled_at IS NULL"),
            sqlite_where=text("disabled_at IS NULL"),
        ),
    )
