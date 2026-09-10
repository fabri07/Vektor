"""ORM: identidad fuerte de operaciones importadas y su vínculo con los efectos.

Dos tablas y no una, por una razón concreta: la identidad es del DOCUMENTO y los
efectos son las FILAS. Un remito de tres renglones es una identidad y tres gastos.
Con una sola tabla (una columna ``entity_id`` en la identidad) no habría forma de
liberar la identidad cuando se revierten dos de los tres renglones y el tercero se
conserva porque el usuario lo editó — que es exactamente lo que pasa en una
relectura, y liberar de más significa que el próximo import vuelve a aplicar un
efecto que todavía está vivo.

Por qué acá y no en ``custom_fields``
-------------------------------------
La deduplicación necesita dos cosas que un JSONB no da: un **UNIQUE** que dos
cargas simultáneas no puedan atravesar (la reclamación es lo que hace de candado)
y un índice para resolver miles de claves en una query. Guardarla en
``sales_entries.custom_fields`` habría obligado a un scan por import.

Ver ``domain/operation_identity.py`` para QUÉ es una clave fuerte y por qué es tan
exigente.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base


class OperationIdentity(Base):
    """Una operación identificada por clave fuerte, con la huella de su contenido.

    ``identity_key`` es la clave canónica que produce
    ``domain.operation_identity.clave_de_operacion`` (con su versión adentro).
    ``content_hash`` es lo que distingue "ya está aplicada" de "misma identidad,
    contenido distinto" — el conflicto, que va a revisión y nunca se omite.
    """

    __tablename__ = "operation_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Clave canónica SIN el tenant: el aislamiento lo da el unique compuesto, no
    #: el texto. Así dos tenants con el mismo número de comprobante no se ven.
    identity_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    #: Sólo traza: de qué archivo vino la primera vez. NO se usa para liberar
    #: —para eso están los vínculos—, porque un archivo borrado puede haber
    #: dejado efectos vivos que el usuario editó.
    first_seen_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "identity_key", name="uq_operation_identities_tenant_key"),
    )


class OperationIdentityLink(Base):
    """Qué efecto persistido produjo una identidad.

    Es lo que permite liberar SELECTIVAMENTE: se borra el vínculo de cada efecto
    revertido y la identidad se libera sólo cuando no le queda ninguno. Un
    archivo borrado que dejó un renglón vivo (porque el usuario lo editó) NO
    libera la identidad — si la liberara, el próximo import volvería a aplicar
    ese renglón encima del que quedó.
    """

    __tablename__ = "operation_identity_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("operation_identities.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Denormalizado a propósito: las liberaciones filtran por tenant + entidad y
    #: sin esta columna cada una tendría que pasar por un JOIN con la identidad.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "identity_id", "entity_type", "entity_id", name="uq_operation_identity_links_efecto"
        ),
        sa.Index("ix_operation_identity_links_efecto", "tenant_id", "entity_type", "entity_id"),
    )
