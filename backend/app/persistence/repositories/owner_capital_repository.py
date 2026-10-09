"""Lecturas de capital de los socios para los agregados de CAJA (D·2).

Vive en persistencia (y no en el servicio) porque la consumen la capa de estado
(``business_state_service``) y dos servicios: arqueo y proyección de caja.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.owner_capital import (
    FORM_DINERO,
    KINDS_ENTRADA,
    KINDS_SALIDA,
    OwnerCapitalMovement,
)


@dataclass(frozen=True)
class FlujoDeCapital:
    """Lo que entró y salió de capital en dinero un día, por medio de pago."""

    dia: date
    medio: str
    entradas: Decimal
    salidas: Decimal


async def flujo_de_dinero(
    session: AsyncSession, tenant_id: uuid.UUID, desde: date, hasta: date
) -> list[FlujoDeCapital]:
    """Capital en DINERO, vivo, por día y medio. Sólo para agregados de caja."""
    m = OwnerCapitalMovement
    entradas = func.sum(case((m.kind.in_(KINDS_ENTRADA), m.amount_ars), else_=0))
    salidas = func.sum(case((m.kind.in_(KINDS_SALIDA), m.amount_ars), else_=0))
    filas = (
        await session.execute(
            select(m.occurred_on, m.payment_method, entradas, salidas)
            .where(
                m.tenant_id == tenant_id,
                m.voided_at.is_(None),
                m.form == FORM_DINERO,
                m.occurred_on >= desde,
                m.occurred_on <= hasta,
            )
            .group_by(m.occurred_on, m.payment_method)
        )
    ).all()
    return [
        FlujoDeCapital(
            dia=date.fromisoformat(str(dia)[:10]),
            medio=str(medio),
            entradas=Decimal(str(ent or 0)),
            salidas=Decimal(str(sal or 0)),
        )
        for dia, medio, ent, sal in filas
    ]
