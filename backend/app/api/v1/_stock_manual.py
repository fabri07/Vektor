"""Traducción HTTP de la carga manual de stock (M·P2c).

La comparten ``products`` y ``others``: los dos dan de alta o corrigen stock a
mano y tienen que responder igual.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.domain.business_time import a_hora_de_negocio, now_ar_naive
from app.domain.contrato_de_costo import contrato_declarado
from app.domain.costo_de_referencia import Tratamiento


def exigir_costo_con_stock(entra_stock: bool, costo: Decimal | None) -> None:
    try:
        stock_service.exigir_costo_con_stock(entra_stock, costo)
    except stock_service.CostoRequeridoError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "COSTO_REQUERIDO_CON_STOCK", "message": str(exc)},
        ) from exc


def exigir_fecha_con_stock(entra_stock: bool, fecha: datetime | None) -> None:
    """M·2d: un alta con stock inicial dice desde cuándo lo tiene el negocio.

    La fecha la da el usuario: no se infiere de la carga ni de nada más.
    """
    if entra_stock and fecha is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "FECHA_STOCK_INICIAL_REQUERIDA",
                "message": (
                    "Indicá desde qué fecha tenés este stock inicial (acquired_at)."
                ),
            },
        )


def momento_del_costo_del_alta(stock_inicial: int, fecha: datetime | None) -> datetime:
    """Hora con la que el alta sella el costo tipeado como edición manual (M·1c).

    Con stock inicial, la MISMA fecha que la apertura (nunca posterior a ahora):
    sellarlo «ahora» le ganaría por día a una apertura atrasada
    (``decidir_costo_vigente``) y el costo vigente diría ``manual`` en vez de
    ``stock_inicial``. En el mismo día la apertura gana por hora de registro.
    """
    ahora = now_ar_naive()
    if stock_inicial > 0 and fecha is not None:
        return min(a_hora_de_negocio(fecha), ahora)
    return ahora


async def registrar_apertura_http(
    session: AsyncSession,
    tenant_id: UUID,
    product_id: UUID,
    qty: int,
    *,
    costo: Decimal | None,
    envio: Tratamiento | None,
    fecha: datetime,
    confirmado_por: UUID | None,
    source_upload_id: UUID | None = None,
) -> None:
    await stock_service.registrar_apertura_manual(
        product_id,
        tenant_id,
        qty,
        contrato_declarado(costo, envio=envio or "desconocido"),
        session,
        fecha=fecha,
        confirmado_por=confirmado_por,
        source_upload_id=source_upload_id,
    )


async def aplicar_ajuste_manual_http(
    session: AsyncSession,
    tenant_id: UUID,
    product_id: UUID,
    nuevo: int,
    *,
    esperado: int | None,
    costo: Decimal | None,
    envio: Tratamiento | None,
    actor: str,
) -> None:
    contrato = (
        contrato_declarado(costo, envio=envio or "desconocido") if costo is not None else None
    )
    try:
        await stock_service.registrar_ajuste_manual(
            product_id,
            tenant_id,
            session,
            nuevo=nuevo,
            esperado=esperado,
            contrato=contrato,
            actor=actor,
        )
    except stock_service.StockCambioError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STOCK_CAMBIO",
                "message": (
                    f"El stock cambió mientras editabas (ahora es {exc.actual}). "
                    "Revisalo y volvé a guardar."
                ),
                "stock_actual": exc.actual,
            },
        ) from exc
    except stock_service.StockNegativoError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "STOCK_NEGATIVO", "message": str(exc)},
        ) from exc
