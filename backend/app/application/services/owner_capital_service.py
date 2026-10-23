"""Capital de los socios (D·2): alta, listado, anulación y flujo en dinero.

Un movimiento de capital **no es venta ni gasto**: no entra en FactsService como
gasto, ni en el margen, ni en el resultado. Lo único que lee de acá el resto del
sistema es ``owner_capital_repository.flujo_de_dinero``, y sólo los agregados de CAJA (arqueo,
liquidez por flujo, proyección de caja): la plata de un retiro salió del cajón
igual que la de un gasto, y esconderla ahí haría un arqueo que no cuadra
(misma regla que ``_expense_scope.py``).

El ``tenant_id`` lo pone siempre quien llama, desde el JWT; este módulo nunca lo
lee de un cuerpo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import (
    FORM_DINERO,
    SOURCE_MANUAL,
    OwnerCapitalMovement,
)

AUDIT_CREATED = "OWNER_CAPITAL_MOVEMENT_CREATED"
AUDIT_VOIDED = "OWNER_CAPITAL_MOVEMENT_VOIDED"


class CapitalMovementNotFoundError(Exception):
    """No existe en ESTE tenant (un id de otro tenant es indistinguible)."""


class CapitalMovementAlreadyVoidedError(Exception):
    """Ya estaba anulado: anular dos veces pisaría quién y por qué."""


def _snapshot(m: OwnerCapitalMovement) -> dict[str, object]:
    return {
        "id": str(m.id),
        "kind": m.kind,
        "form": m.form,
        "amount_ars": str(m.amount_ars),
        "occurred_on": m.occurred_on.isoformat(),
        "payment_method": m.payment_method,
        "partner_name": m.partner_name,
        "notes": m.notes,
        "source_type": m.source_type,
        "source_ref": m.source_ref,
        "voided_at": m.voided_at.isoformat() if m.voided_at else None,
        "void_reason": m.void_reason,
    }


def _audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    decision_type: str,
    before: dict[str, object] | None,
    after: dict[str, object],
    endpoint: str,
    triggered_by: str = "ui:owner_capital",
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "record_type": "owner_capital_movement",
                "record_id": after["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by=triggered_by,
            actor_user_id=user_id,
            context={"endpoint": endpoint},
            created_at=datetime.now(UTC),
        )
    )


async def registrar_movimiento(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    amount: Decimal,
    occurred_on: date,
    payment_method: str,
    partner_name: str | None = None,
    notes: str | None = None,
    endpoint: str = "capital-movements",
    source_type: str = SOURCE_MANUAL,
    triggered_by: str = "ui:owner_capital",
) -> OwnerCapitalMovement:
    """Alta de un movimiento en dinero, con su auditoría. No commitea.

    ``source_type`` lo fija quien llama, nunca el cliente: la API sólo da de alta
    ``manual``; el onboarding (D·5) da de alta ``onboarding_declarado``.
    """
    mov = OwnerCapitalMovement(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        form=FORM_DINERO,
        amount_ars=amount,
        occurred_on=occurred_on,
        payment_method=payment_method,
        partner_name=partner_name,
        notes=notes,
        source_type=source_type,
        created_by_user_id=user_id,
    )
    session.add(mov)
    await session.flush()
    _audit(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        decision_type=AUDIT_CREATED,
        before=None,
        after=_snapshot(mov),
        endpoint=endpoint,
        triggered_by=triggered_by,
    )
    return mov


async def listar_movimientos(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    from_date: date | None = None,
    to_date: date | None = None,
    kind: str | None = None,
    include_voided: bool = False,
    limit: int = 200,
) -> list[OwnerCapitalMovement]:
    q = select(OwnerCapitalMovement).where(OwnerCapitalMovement.tenant_id == tenant_id)
    if from_date is not None:
        q = q.where(OwnerCapitalMovement.occurred_on >= from_date)
    if to_date is not None:
        q = q.where(OwnerCapitalMovement.occurred_on <= to_date)
    if kind is not None:
        q = q.where(OwnerCapitalMovement.kind == kind)
    if not include_voided:
        q = q.where(OwnerCapitalMovement.voided_at.is_(None))
    q = q.order_by(
        OwnerCapitalMovement.occurred_on.desc(), OwnerCapitalMovement.created_at.desc()
    ).limit(limit)
    return list((await session.execute(q)).scalars().all())


async def anular_movimiento(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    movement_id: uuid.UUID,
    reason: str,
) -> OwnerCapitalMovement:
    """Anula (sin borrar) y audita. No commitea."""
    mov = (
        await session.execute(
            select(OwnerCapitalMovement)
            .where(
                OwnerCapitalMovement.id == movement_id,
                OwnerCapitalMovement.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mov is None:
        raise CapitalMovementNotFoundError(str(movement_id))
    if mov.voided_at is not None:
        raise CapitalMovementAlreadyVoidedError(str(movement_id))
    before = _snapshot(mov)
    mov.voided_at = datetime.now(UTC)
    mov.voided_by_user_id = user_id
    mov.void_reason = reason
    await session.flush()
    _audit(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        decision_type=AUDIT_VOIDED,
        before=before,
        after=_snapshot(mov),
        endpoint="capital-movements/void",
    )
    return mov
