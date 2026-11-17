"""Alias de cliente confirmados por una persona («Caro G.» → Carolina Giménez).

Mismo contrato que el alias de producto (B·8a): lo aprende una persona al resolver
desde «Otros» una venta fiada o un cobro eligiendo el cliente; el importador lo
consulta antes de mandar un fiado sin cliente a «Otros». Nunca se aprende solo ni se
fusionan clientes: es un nombre más que identifica a UNA ficha.

Aprender nunca rompe lo que se está resolviendo: ante un conflicto no se escribe nada
y se devuelve el motivo (un alias que ya es de OTRO cliente, o que es el nombre de otra
ficha, no identificaría a nadie).
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.text_norm import normalize_text
from app.observability.logger import get_logger
from app.persistence.models.customer import Customer
from app.persistence.models.customer_alias import CUSTOMER_ALIAS_ORIGIN_MANUAL, CustomerAlias

logger = get_logger(__name__)

CREADO: Final = "creado"
YA_EXISTIA: Final = "ya_existia"
SIN_EFECTO: Final = "sin_efecto"
CONFLICTO_ALIAS: Final = "conflicto_alias"
CONFLICTO_NOMBRE: Final = "conflicto_nombre"

_MAX = 300


def _nombres_de(c: Customer) -> set[str]:
    """Las formas normalizadas con que la ficha ya se nombra sola."""
    formas = {normalize_text(c.name or "")}
    if (c.last_name or "").strip():
        formas.add(normalize_text(f"{c.name} {c.last_name}"))
    return {f for f in formas if f}


async def indice_de_alias(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """``{alias normalizado: cliente}`` de las fichas ACTIVAS y reales del negocio."""
    filas = await session.execute(
        select(CustomerAlias.alias_normalizado, CustomerAlias.customer_id)
        .join(Customer, Customer.id == CustomerAlias.customer_id)
        .where(
            CustomerAlias.tenant_id == tenant_id,
            Customer.tenant_id == tenant_id,
            Customer.deactivated_at.is_(None),
        )
    )
    return dict(filas.tuples().all())


async def cliente_por_alias(
    session: AsyncSession, tenant_id: uuid.UUID, texto: str | None
) -> uuid.UUID | None:
    """El cliente al que apunta un alias confirmado, o ``None``."""
    alias_n = normalize_text(texto or "")
    if not alias_n:
        return None
    return (await indice_de_alias(session, tenant_id)).get(alias_n)


async def aprender_alias(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    texto: str | None,
    customer_id: uuid.UUID,
) -> str:
    """Guarda ``texto`` como otro nombre de ``customer_id``. No commitea.

    Devuelve qué pasó: ``creado``, ``ya_existia`` (ya era de este cliente),
    ``sin_efecto`` (vacío, demasiado largo, centinela o ya es su propio nombre) o un
    conflicto (``conflicto_alias``: ya es de OTRO cliente; ``conflicto_nombre``: es el
    nombre de OTRA ficha). En un conflicto no se escribe nada.
    """
    crudo = (texto or "").strip()
    alias_n = normalize_text(crudo)
    if not alias_n or len(alias_n) > _MAX or len(crudo) > _MAX:
        return SIN_EFECTO
    cliente = (
        await session.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if cliente is None or cliente.is_sentinel:
        return SIN_EFECTO
    if alias_n in _nombres_de(cliente):
        return SIN_EFECTO

    activas = (
        await session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
                Customer.id != customer_id,
            )
        )
    ).scalars()
    for otra in activas:
        if not otra.is_sentinel and alias_n in _nombres_de(otra):
            logger.info("customer_alias.conflicto_nombre", tenant_id=str(tenant_id))
            return CONFLICTO_NOMBRE

    existente = (
        await session.execute(
            select(CustomerAlias).where(
                CustomerAlias.tenant_id == tenant_id,
                CustomerAlias.alias_normalizado == alias_n,
            )
        )
    ).scalar_one_or_none()
    if existente is not None:
        return YA_EXISTIA if existente.customer_id == customer_id else CONFLICTO_ALIAS
    try:
        async with session.begin_nested():
            session.add(
                CustomerAlias(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    alias=crudo,
                    alias_normalizado=alias_n,
                    origin=CUSTOMER_ALIAS_ORIGIN_MANUAL,
                    created_by_user_id=user_id,
                )
            )
            await session.flush()
    except IntegrityError:
        # Otra resolución lo aprendió entre la lectura y el alta.
        ganador = (
            await session.execute(
                select(CustomerAlias.customer_id).where(
                    CustomerAlias.tenant_id == tenant_id,
                    CustomerAlias.alias_normalizado == alias_n,
                )
            )
        ).scalar_one_or_none()
        return YA_EXISTIA if ganador == customer_id else CONFLICTO_ALIAS
    return CREADO
