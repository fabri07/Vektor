"""Caja (POS): la operación como unidad con identidad propia.

Se apoya en lo que ya existe —bloqueo de filas por producto, validación de
stock, descuento idempotente por línea, idempotencia recuperable de B2— y
agrega lo que faltaba: una cabecera referenciable, los pagos colgando de ella y
un descuento que cierra al centavo.

**El cliente no fija precios.** ``unit_price_list`` lo pone el servidor desde el
catálogo bloqueado; el cuerpo de la petición sólo dice qué producto, cuánto y
qué descuento. Entre el navegador y este handler hay una cola offline que se
puede editar a mano, así que un carrito que llegara con sus propios importes
sería un carrito que puede cobrar de menos.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_active_subscription,
    require_modify_access,
    require_role,
)

# Se importa la resolución de cliente en vez de repetirla: la regla de que el
# fiado exige un cliente real (nunca el centinela "Local") es una regla de
# plata, y dos copias que pueden divergir es peor que este acoplamiento.
from app.api.v1.sales import (
    FIADO_PAYMENT_METHOD,
    _audit_data_change,
    _resolve_sale_customer,
    _sale_snapshot,
)
from app.application.services import stock_service
from app.application.services.idempotency import (
    ClaveReusada,
    Repeticion,
    claim_idempotent_request,
    record_idempotent_response,
)
from app.application.services.maintenance_lock_service import acquire_write_lock_shared
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.domain.pos_operation import (
    LineaPedida,
    OperacionInvalidaError,
    calcular_operacion,
    calcular_vuelto,
    validar_tenders,
)
from app.persistence.db.session import get_db_session
from app.persistence.models import (
    PosOperation,
    PosOperationLine,
    PosTender,
    Product,
    SaleEntry,
    Tenant,
    User,
)
from app.persistence.models.audit import DecisionAuditLog
from app.schemas.pos import (
    PosOperationRequest,
    PosOperationResponse,
    PosVoidRequest,
)

router = APIRouter()

_ACCION_IDEMPOTENTE = "IDEMPOTENT_POST_POS_OPERATION"

#: Motivo de anulación de las `SaleEntry` de un ticket anulado desde la caja.
#: Ya está en el CHECK `ck_sales_entries_void_reason`: no hace falta migrar.
_VOID_REASON_TICKET = "USER_CANCELLED"


def _a_respuesta(
    operacion: PosOperation,
    lineas: list[PosOperationLine],
    tenders: list[PosTender],
) -> PosOperationResponse:
    return PosOperationResponse(
        id=operacion.id,
        client_operation_id=operacion.client_operation_id,
        created_by_user_id=operacion.created_by_user_id,
        customer_id=operacion.customer_id,
        operation_date=operacion.operation_date,
        subtotal_ars=operacion.subtotal_ars,
        discount_ars=operacion.discount_ars,
        total_ars=operacion.total_ars,
        cash_received_ars=operacion.cash_received_ars,
        cash_change_ars=operacion.cash_change_ars,
        status=operacion.status,
        notes=operacion.notes,
        # Pydantic valida cada ORM contra su schema por `from_attributes`.
        lines=lineas,
        tenders=tenders,
    )


async def _cargar_operacion(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    operation_id: uuid.UUID | None = None,
    client_operation_id: str | None = None,
    para_actualizar: bool = False,
) -> tuple[PosOperation, list[PosOperationLine], list[PosTender]] | None:
    """Cabecera + líneas + pagos, en orden de posición. ``None`` si no existe."""
    stmt = select(PosOperation).where(PosOperation.tenant_id == tenant_id)
    if operation_id is not None:
        stmt = stmt.where(PosOperation.id == operation_id)
    else:
        stmt = stmt.where(PosOperation.client_operation_id == client_operation_id)
    if para_actualizar:
        stmt = stmt.with_for_update()
    operacion = (await session.execute(stmt)).scalar_one_or_none()
    if operacion is None:
        return None
    lineas = list(
        (
            await session.execute(
                select(PosOperationLine)
                .where(PosOperationLine.operation_id == operacion.id)
                .order_by(PosOperationLine.position)
            )
        )
        .scalars()
        .all()
    )
    tenders = list(
        (
            await session.execute(
                select(PosTender)
                .where(PosTender.operation_id == operacion.id)
                .order_by(PosTender.position)
            )
        )
        .scalars()
        .all()
    )
    return operacion, lineas, tenders


@router.post(
    "/operations",
    response_model=PosOperationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar una operación de caja (líneas + pagos, atómica)",
)
async def create_pos_operation(
    body: PosOperationRequest,
    background: BackgroundTasks,
    response: Response,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    _sub_guard: None = Depends(require_active_subscription),
    session: AsyncSession = Depends(get_db_session),
) -> PosOperationResponse:
    """Cobra un carrito: una `SaleEntry` por línea, más la cabecera y sus pagos.

    Atómica: si una línea falla, no queda ni media venta ni medio descuento de
    stock. La clave de idempotencia es el `client_operation_id` del cuerpo —lo
    genera la caja antes del primer envío—, así que un reintento tras una
    respuesta perdida devuelve el ticket original en vez de cobrar de nuevo.
    """
    # La idempotencia va PRIMERO: antes de tocar stock, precios o locks. Un
    # reintento tiene que poder contestar sin repetir nada de eso.
    reclamo = await claim_idempotent_request(
        session, tenant.tenant_id, body.client_operation_id, _ACCION_IDEMPOTENTE, body
    )
    if isinstance(reclamo, ClaveReusada):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": (
                    "Ese id de operación ya se usó para otra venta. Un reintento "
                    "tiene que mandar exactamente el mismo contenido."
                ),
            },
        )
    if isinstance(reclamo, Repeticion):
        # 200 y no 201: el ticket ya existía y esta petición no creó nada.
        response.status_code = status.HTTP_200_OK
        # Se devuelve el estado ACTUAL del ticket, no el snapshot guardado al
        # crearlo: si entre medio se anuló, un reintento tardío de la cola
        # offline no puede recibir `COMPLETED` y reimprimir un ticket muerto. El
        # snapshot queda como respaldo por si la operación ya no existiera.
        actual = await _cargar_operacion(
            session, tenant.tenant_id, client_operation_id=body.client_operation_id
        )
        if actual is not None:
            return _a_respuesta(*actual)
        if reclamo.respuesta is None:
            raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
        return PosOperationResponse.model_validate(reclamo.respuesta)

    # Pago mixto: la tabla lo soporta desde ya, pero los ocho lectores de caja
    # todavía leen `payment_method` de la venta y no conocen los tenders. Hasta
    # que B9 los cablee, aceptar un mixto produciría ventas cuya plata ningún
    # arqueo puede imputar. Se rechaza explícito en vez de generar el agujero.
    if len(body.tenders) > 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MIXED_TENDER_NOT_ENABLED",
                "message": "El pago mixto todavía no está habilitado.",
            },
        )

    # El advisory shared SIEMPRE antes que cualquier FOR UPDATE de fila: si no,
    # deadlockea AB-BA contra el exclusive del script de dedup.
    await acquire_write_lock_shared(session, tenant.tenant_id)

    product_ids = [item.product_id for item in body.items]
    if len(set(product_ids)) != len(product_ids):
        raise HTTPException(
            status_code=400,
            detail="Hay productos repetidos en la venta. Unificá la cantidad en una sola línea.",
        )

    metodo_unico = body.tenders[0].payment_method
    # El vuelto se calcula contra la parte EN EFECTIVO. Sin pago en efectivo, un
    # "entregado" daría como vuelto el monto entero ($10.000 de vuelto sobre una
    # venta con tarjeta) y cualquier lector de caja que use el vuelto se rompe.
    if body.cash_received_ars is not None and not any(
        t.payment_method == "cash" for t in body.tenders
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CASH_RECEIVED_WITHOUT_CASH_TENDER",
                "message": "Se informó efectivo entregado pero la venta no se paga en efectivo.",
            },
        )
    customer_id = await _resolve_sale_customer(
        session,
        tenant.tenant_id,
        body.customer_id,
        is_fiado=metodo_unico == FIADO_PAYMENT_METHOD,
    )

    # Bloqueo en orden estable (por id): anti-deadlock y anti-sobreventa.
    locked = (
        (
            await session.execute(
                select(Product)
                .where(
                    Product.tenant_id == tenant.tenant_id,
                    Product.id.in_(sorted(product_ids, key=str)),
                    Product.is_active.is_(True),
                )
                .order_by(Product.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    por_id = {p.id: p for p in locked}

    pedidas: list[LineaPedida] = []
    for item in body.items:
        producto = por_id.get(item.product_id)
        if producto is None:
            raise HTTPException(
                status_code=400, detail=f"Producto {item.product_id} no encontrado."
            )
        # Un producto sin precio (los que crea una compra importada nacen con
        # `sale_price_ars = 0` y `requires_completion`) no se puede cobrar: se
        # regalaría y se descontaría su stock. Se rechaza con un motivo claro en
        # vez de dejar que aparezca como un TENDERS_MISMATCH incomprensible.
        if producto.sale_price_ars <= 0:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PRODUCT_WITHOUT_PRICE",
                    "product_id": str(producto.id),
                    "message": f"«{producto.name}» no tiene precio de venta cargado.",
                },
            )
        if producto.stock_units < item.quantity:
            raise stock_service.InsufficientStockError(
                item.product_id, producto.stock_units, item.quantity, producto.name
            )
        pedidas.append(
            LineaPedida(
                product_id=item.product_id,
                quantity=item.quantity,
                # Del CATÁLOGO, no del cuerpo de la petición.
                unit_price_list=producto.sale_price_ars,
                discount_ars=item.discount_ars,
            )
        )

    # Toda la aritmética en un módulo puro: el reparto de centavos es lo que
    # hace que la suma de las líneas sea exactamente lo que se cobra.
    try:
        calculada = calcular_operacion(pedidas, body.discount_ars)
        validar_tenders(calculada.total, [t.amount_ars for t in body.tenders])
        efectivo = sum(
            (t.amount_ars for t in body.tenders if t.payment_method == "cash"),
            Decimal("0"),
        )
        vuelto = calcular_vuelto(body.cash_received_ars, efectivo)
    except OperacionInvalidaError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.CODE, "message": str(exc)}
        ) from exc

    operacion = PosOperation(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        client_operation_id=body.client_operation_id,
        created_by_user_id=user.user_id,
        customer_id=customer_id,
        operation_date=body.operation_date,
        subtotal_ars=calculada.subtotal,
        discount_ars=calculada.discount_global_ars,
        total_ars=calculada.total,
        cash_received_ars=body.cash_received_ars,
        # Entregado y vuelto van juntos: el CHECK de la tabla lo exige, porque
        # un vuelto sin entregado no se puede explicar.
        cash_change_ars=vuelto if body.cash_received_ars is not None else None,
        notes=body.notes,
    )
    session.add(operacion)

    lineas: list[PosOperationLine] = []
    tenders: list[PosTender] = []
    for posicion, linea in enumerate(calculada.lineas):
        venta = SaleEntry(
            tenant_id=tenant.tenant_id,
            amount=linea.line_total,
            quantity=linea.quantity,
            # El precio de LISTA, no el efectivo: el descuento vive en la línea
            # de la operación. Derivar un unitario del total sería la división
            # prohibida, y con descuento indivisible ni siquiera existe.
            unit_price=linea.unit_price_list,
            transaction_date=body.operation_date,
            payment_method=metodo_unico,
            product_id=linea.product_id,
            customer_id=customer_id,
            created_by_user_id=user.user_id,
            notes=body.notes,
        )
        session.add(venta)
        await session.flush()
        await stock_service.decrement_for_sale(venta, session)

        lineas.append(
            PosOperationLine(
                id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                operation_id=operacion.id,
                sale_entry_id=venta.id,
                product_id=linea.product_id,
                position=posicion,
                quantity=linea.quantity,
                unit_price_list=linea.unit_price_list,
                discount_line_ars=linea.discount_line_ars,
                discount_global_share_ars=linea.discount_global_share_ars,
                line_total_ars=linea.line_total,
            )
        )

    for posicion, tender in enumerate(body.tenders):
        tenders.append(
            PosTender(
                id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                operation_id=operacion.id,
                position=posicion,
                payment_method=tender.payment_method,
                amount_ars=tender.amount_ars,
            )
        )

    session.add_all(lineas)
    session.add_all(tenders)
    await session.flush()

    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "pos_operation_created", background
    )

    resultado = _a_respuesta(operacion, lineas, tenders)
    # En la MISMA transacción que la venta: guardarla después dejaría un ticket
    # persistido cuyo reintento no recupera nada.
    await record_idempotent_response(
        session,
        tenant.tenant_id,
        body.client_operation_id,
        resultado,
        status.HTTP_201_CREATED,
    )
    return resultado


@router.post(
    "/operations/{operation_id}/void",
    response_model=PosOperationResponse,
    summary="Anular un ticket de caja completo (líneas + stock, atómico)",
)
async def void_pos_operation(
    operation_id: uuid.UUID,
    background: BackgroundTasks,
    body: PosVoidRequest | None = None,
    tenant: Tenant = Depends(get_current_tenant),
    # Mismo gate que anular una venta suelta: OWNER o sub-cuenta con permiso, y
    # ventana de PIN. B5 lo pasa a un permiso de cajero explícito.
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> PosOperationResponse:
    """Anula el ticket ENTERO: todas sus ventas y el stock que descontaron.

    Sólo existe la anulación completa (decisión de v1: sin devolución parcial).
    Es la ÚNICA vía para anular una línea de caja — el PATCH/DELETE genérico de
    ventas devuelve 409 ``BELONGS_TO_POS_OPERATION`` — porque una línea suelta
    anulada deja un ticket con sus pagos intactos que ningún arqueo cierra.

    Idempotente: anular un ticket ya anulado devuelve el mismo ticket y no
    revierte stock de nuevo. Los `pos_tenders` NO se borran: el ticket anulado
    tiene que poder reimprimirse y auditarse, y `status = VOIDED` es lo que dice
    que esos pagos ya no cuentan.
    """
    # El advisory shared SIEMPRE antes que cualquier FOR UPDATE de fila.
    await acquire_write_lock_shared(session, tenant.tenant_id)

    # FOR UPDATE sobre la cabecera: dos anulaciones simultáneas del mismo ticket
    # se serializan acá, y la segunda ve `VOIDED` y no revierte stock otra vez.
    cargada = await _cargar_operacion(
        session, tenant.tenant_id, operation_id=operation_id, para_actualizar=True
    )
    if cargada is None:
        raise HTTPException(status_code=404, detail="Operación no encontrada.")
    operacion, lineas, tenders = cargada

    if operacion.status == "VOIDED":
        return _a_respuesta(operacion, lineas, tenders)

    ahora = datetime.now(UTC)
    ventas_anuladas: list[str] = []
    ids_de_venta = [ln.sale_entry_id for ln in lineas if ln.sale_entry_id is not None]
    ventas = (
        (
            await session.execute(
                select(SaleEntry)
                .where(
                    SaleEntry.id.in_(ids_de_venta),
                    SaleEntry.tenant_id == tenant.tenant_id,
                    SaleEntry.voided_at.is_(None),
                )
                .order_by(SaleEntry.id)
            )
        )
        .scalars()
        .all()
        if ids_de_venta
        else []
    )
    # En el orden del ticket, para que la auditoría liste las ventas como el
    # cajero las cargó y no en el orden de los UUID.
    posicion = {ln.sale_entry_id: ln.position for ln in lineas}
    for venta in sorted(ventas, key=lambda v: posicion[v.id]):
        antes = _sale_snapshot(venta)
        venta.voided_at = ahora
        venta.void_reason = _VOID_REASON_TICKET
        await session.flush()
        # Reversa incremental del movimiento de esta línea (`void_movement`). Nunca
        # un `setattr` sobre stock_units: el stock no es la suma del ledger.
        await stock_service.revert_sale_stock(venta.id, tenant.tenant_id, session)
        # Mismo registro que una anulación suelta, para que el historial de la
        # venta cuente lo mismo venga de donde venga.
        _audit_data_change(
            session,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            decision_type="DATA_RECORD_VOIDED",
            before=antes,
            after=_sale_snapshot(venta),
        )
        ventas_anuladas.append(str(venta.id))

    operacion.status = "VOIDED"
    session.add(
        DecisionAuditLog(
            tenant_id=tenant.tenant_id,
            decision_type="POS_OPERATION_VOIDED",
            decision_data={
                "record_type": "pos_operation",
                "record_id": str(operacion.id),
                "client_operation_id": operacion.client_operation_id,
                "total_ars": str(operacion.total_ars),
                "sale_entry_ids": ventas_anuladas,
                "tenders": [
                    {"payment_method": t.payment_method, "amount_ars": str(t.amount_ars)}
                    for t in tenders
                ],
                "reason": body.reason if body is not None else None,
                "source": "pos",
            },
            triggered_by="ui:pos",
            actor_user_id=user.user_id,
            context={"endpoint": "pos.void"},
            created_at=ahora,
        )
    )
    await session.flush()

    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "pos_operation_voided", background
    )
    return _a_respuesta(operacion, lineas, tenders)
