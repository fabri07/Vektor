"""Pagos de deuda a proveedores: alta, anulación, listado, saldo y flujo de caja.

Un pago a un proveedor por una compra a cuenta corriente **no es un gasto ni una
compra** (ver ``persistence/models/supplier_payment.py``): saca plata de la caja y baja
la deuda con el proveedor, sin tocar el costo, el margen ni el resultado. Lo único que
lee de acá el resto del sistema es ``flujo_de_pagos`` —sólo para los agregados de
CAJA: arqueo, liquidez por flujo y proyección—, igual que el capital de los socios.

La deuda con un proveedor NO se guarda: es lo comprado a cuenta corriente menos lo
pagado (``saldo_de_proveedor``), así que no hay un saldo que se desfase.

El ``tenant_id`` lo pone siempre quien llama, desde el JWT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.supplier import Supplier
from app.persistence.models.supplier_payment import SOURCE_MANUAL, SupplierPayment
from app.persistence.models.transaction import ExpenseEntry

AUDIT_CREATED = "SUPPLIER_PAYMENT_CREATED"
AUDIT_VOIDED = "SUPPLIER_PAYMENT_VOIDED"

#: Una compra «a cuenta corriente» es la que deja deuda con el proveedor.
CUENTA_CORRIENTE = "account"


class SupplierPaymentSupplierError(Exception):
    """El proveedor no existe en ESTE tenant, está dado de baja o es un centinela."""


class SupplierPaymentNotFoundError(Exception):
    """No existe en ESTE tenant (un id de otro tenant es indistinguible)."""


class SupplierPaymentAlreadyVoidedError(Exception):
    """Ya estaba anulado: anular dos veces pisaría quién y por qué."""


def _snapshot(p: SupplierPayment) -> dict[str, object]:
    return {
        "id": str(p.id),
        "supplier_id": str(p.supplier_id),
        "amount_ars": str(p.amount_ars),
        "occurred_on": p.occurred_on.isoformat(),
        "payment_method": p.payment_method,
        "notes": p.notes,
        "source_type": p.source_type,
        "source_ref": p.source_ref,
        "voided_at": p.voided_at.isoformat() if p.voided_at else None,
        "void_reason": p.void_reason,
    }


def _audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    decision_type: str,
    before: dict[str, object] | None,
    after: dict[str, object],
    endpoint: str,
    triggered_by: str = "ui:supplier_payment",
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "record_type": "supplier_payment",
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


async def _proveedor_real(
    session: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> Supplier:
    proveedor = (
        await session.execute(
            select(Supplier).where(
                Supplier.id == supplier_id,
                Supplier.tenant_id == tenant_id,
                Supplier.deactivated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if proveedor is None or proveedor.is_sentinel:
        raise SupplierPaymentSupplierError(str(supplier_id))
    return proveedor


async def registrar_pago(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    supplier_id: uuid.UUID,
    amount: Decimal,
    occurred_on: date,
    payment_method: str,
    notes: str | None = None,
    endpoint: str = "suppliers/payments",
    source_type: str = SOURCE_MANUAL,
    source_upload_id: uuid.UUID | None = None,
    source_ref: str | None = None,
    triggered_by: str = "ui:supplier_payment",
) -> SupplierPayment:
    """Alta de un pago a un proveedor real, con su auditoría. No commitea."""
    await _proveedor_real(session, tenant_id, supplier_id)
    pago = SupplierPayment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        supplier_id=supplier_id,
        occurred_on=occurred_on,
        amount_ars=amount,
        payment_method=payment_method,
        notes=notes,
        source_type=source_type,
        source_upload_id=source_upload_id,
        source_ref=source_ref,
        created_by_user_id=user_id,
    )
    session.add(pago)
    await session.flush()
    _audit(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        decision_type=AUDIT_CREATED,
        before=None,
        after=_snapshot(pago),
        endpoint=endpoint,
        triggered_by=triggered_by,
    )
    return pago


async def anular_pago(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    supplier_id: uuid.UUID,
    payment_id: uuid.UUID,
    reason: str,
) -> SupplierPayment:
    """Anula (sin borrar) y audita. No commitea."""
    pago = (
        await session.execute(
            select(SupplierPayment)
            .where(
                SupplierPayment.id == payment_id,
                SupplierPayment.tenant_id == tenant_id,
                SupplierPayment.supplier_id == supplier_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if pago is None:
        raise SupplierPaymentNotFoundError(str(payment_id))
    if pago.voided_at is not None:
        raise SupplierPaymentAlreadyVoidedError(str(payment_id))
    before = _snapshot(pago)
    pago.voided_at = datetime.now(UTC)
    pago.voided_by_user_id = user_id
    pago.void_reason = reason
    await session.flush()
    _audit(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        decision_type=AUDIT_VOIDED,
        before=before,
        after=_snapshot(pago),
        endpoint="suppliers/payments/void",
    )
    return pago


async def anular_de_archivo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    user_id: uuid.UUID | None,
    reason: str,
) -> int:
    """Anula los pagos vivos que cargó un archivo (la reversa de borrarlo)."""
    pagos = (
        await session.execute(
            select(SupplierPayment)
            .where(
                SupplierPayment.tenant_id == tenant_id,
                SupplierPayment.source_upload_id == file_id,
                SupplierPayment.voided_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    for pago in pagos:
        before = _snapshot(pago)
        pago.voided_at = datetime.now(UTC)
        pago.voided_by_user_id = user_id
        pago.void_reason = reason
        await session.flush()
        _audit(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            decision_type=AUDIT_VOIDED,
            before=before,
            after=_snapshot(pago),
            endpoint="ingestion/files/delete",
            triggered_by="ui:file_deletion",
        )
    return len(pagos)


async def listar_pagos(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    *,
    incluir_anulados: bool = False,
    limite: int = 200,
) -> list[SupplierPayment]:
    consulta = select(SupplierPayment).where(
        SupplierPayment.tenant_id == tenant_id, SupplierPayment.supplier_id == supplier_id
    )
    if not incluir_anulados:
        consulta = consulta.where(SupplierPayment.voided_at.is_(None))
    consulta = consulta.order_by(
        SupplierPayment.occurred_on.desc(), SupplierPayment.created_at.desc()
    ).limit(limite)
    return list((await session.execute(consulta)).scalars().all())


@dataclass(frozen=True)
class SaldoDeProveedor:
    """Lo que se le debe a un proveedor: comprado a cuenta corriente menos pagado."""

    comprado_a_cuenta_corriente: Decimal
    pagado: Decimal

    @property
    def deuda(self) -> Decimal:
        return self.comprado_a_cuenta_corriente - self.pagado


async def saldo_de_proveedor(
    session: AsyncSession, tenant_id: uuid.UUID, supplier_id: uuid.UUID
) -> SaldoDeProveedor:
    """Compras vivas del proveedor a cuenta corriente menos sus pagos vivos. Puede dar
    negativo (se pagó de más o falta cargar la compra): se muestra, no se esconde."""
    comprado = await session.scalar(
        select(func.coalesce(func.sum(ExpenseEntry.amount), 0)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.supplier_id == supplier_id,
            ExpenseEntry.voided_at.is_(None),
            func.lower(ExpenseEntry.payment_method) == CUENTA_CORRIENTE,
        )
    )
    pagado = await session.scalar(
        select(func.coalesce(func.sum(SupplierPayment.amount_ars), 0)).where(
            SupplierPayment.tenant_id == tenant_id,
            SupplierPayment.supplier_id == supplier_id,
            SupplierPayment.voided_at.is_(None),
        )
    )
    return SaldoDeProveedor(Decimal(str(comprado or 0)), Decimal(str(pagado or 0)))


@dataclass(frozen=True)
class FlujoDePagos:
    """Lo que salió de la caja por pagos a proveedores un día, por medio de pago."""

    dia: date
    medio: str
    salidas: Decimal


async def flujo_de_pagos(
    session: AsyncSession, tenant_id: uuid.UUID, desde: date, hasta: date
) -> list[FlujoDePagos]:
    """Pagos vivos por día y medio. Sólo para agregados de CAJA (arqueo, liquidez,
    proyección): un pago no es gasto, pero la plata salió igual."""
    p = SupplierPayment
    filas = await session.execute(
        select(p.occurred_on, p.payment_method, func.sum(p.amount_ars))
        .where(
            p.tenant_id == tenant_id,
            p.voided_at.is_(None),
            p.occurred_on >= desde,
            p.occurred_on <= hasta,
        )
        .group_by(p.occurred_on, p.payment_method)
    )
    return [
        FlujoDePagos(dia=dia, medio=medio, salidas=Decimal(str(total or 0)))
        for dia, medio, total in filas.all()
    ]
