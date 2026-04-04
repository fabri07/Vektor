"""ORM model: business_heuristic_overrides.

Permite que cada negocio (tenant) personalice sus rangos heurísticos.
"""

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin


class BusinessHeuristicOverride(TimestampMixin, Base):
    __tablename__ = "business_heuristic_overrides"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    param_key: Mapped[str] = mapped_column(Text, nullable=False)
    param_value: Mapped[dict] = mapped_column(PGJSONB, nullable=False)

    def __repr__(self) -> str:
        return f"<BusinessHeuristicOverride tenant={self.tenant_id} key={self.param_key!r}>"
