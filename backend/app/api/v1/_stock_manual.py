"""Traducción HTTP de la carga manual de stock (M·P2c).

La comparten ``products`` y ``others``: los dos dan de alta o corrigen stock a
mano y tienen que responder igual.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.domain.business_time import a_hora_de_negocio, now_ar_naive
from app.domain.contrato_de_costo import contrato_declarado
from app.domain.costo_de_referencia import Tratamiento
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement


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
    exigir_sin_apertura: bool = False,
) -> InventoryMovement:
    """``exigir_sin_apertura`` (B·5c): stock inicial de un producto que ya
    existía; 404 / 409 ``PRODUCT_INACTIVE`` / 409 ``OPENING_EXISTS``."""
    try:
        return await stock_service.registrar_apertura_manual(
            product_id,
            tenant_id,
            qty,
            contrato_declarado(costo, envio=envio or "desconocido"),
            session,
            fecha=fecha,
            confirmado_por=confirmado_por,
            source_upload_id=source_upload_id,
            exigir_sin_apertura=exigir_sin_apertura,
        )
    except stock_service.AperturaExistenteError as exc:
        raise await apertura_existente_http(session, exc.apertura) from exc
    except stock_service.ProductoInactivoError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PRODUCT_INACTIVE",
                "message": "El producto está dado de baja: reactivalo para cargarle stock.",
            },
        ) from exc
    except ValueError as exc:
        if not exigir_sin_apertura:
            raise
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found."
        ) from exc


async def apertura_existente_http(
    session: AsyncSession, apertura: InventoryMovement
) -> HTTPException:
    """409 ``OPENING_EXISTS`` con lo que ya hay y SÓLO los caminos que existen
    para corregirlo (B·5c): la fecha, con «Confirmar apertura»; la cantidad, con
    un ajuste de stock; una apertura de archivo, desde ese archivo. El costo de
    una apertura ya cargada no tiene corrección: no se promete."""
    archivo: str | None = None
    if apertura.source_upload_id is not None:
        archivo = (
            await session.execute(
                select(UploadedFile.original_filename).where(
                    UploadedFile.id == apertura.source_upload_id
                )
            )
        ).scalar_one_or_none()
    fecha = apertura.occurred_at or apertura.created_at
    de_archivo = apertura.source_upload_id is not None
    costo = (
        apertura.unit_cost_final if apertura.unit_cost_final is not None else apertura.unit_cost
    )
    caminos = (
        [f"Viene del archivo «{archivo or 'importado'}»: corregila confirmando o "
         "releyendo ese archivo."]
        if de_archivo
        else [
            "Si la fecha está mal, corregila con «Confirmar stock inicial».",
            "Si la cantidad está mal, ajustá el stock desde la edición del producto.",
        ]
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "OPENING_EXISTS",
            "message": "Este producto ya tiene stock inicial cargado.",
            "origen": "archivo" if de_archivo else "manual",
            "archivo": archivo,
            "movement_id": str(apertura.id),
            "fecha": fecha.date().isoformat() if fecha is not None else None,
            "qty": apertura.qty,
            "unit_cost": str(costo) if costo is not None else None,
            "caminos": caminos,
        },
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
