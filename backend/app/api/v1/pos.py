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
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_active_subscription,
    require_role,
)

# Se importa la resolución de cliente en vez de repetirla: la regla de que el
# fiado exige un cliente real (nunca el centinela "Local") es una regla de
# plata, y dos copias que pueden divergir es peor que este acoplamiento.
from app.api.v1.sales import FIADO_PAYMENT_METHOD, _resolve_sale_customer
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
from app.schemas.pos import (
    PosOperationRequest,
    PosOperationResponse,
)

router = APIRouter()

_ACCION_IDEMPOTENTE = "IDEMPOTENT_POST_POS_OPERATION"


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
        if reclamo.respuesta is None:
            raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
        # 200 y no 201: el ticket ya existía y esta petición no creó nada.
        response.status_code = status.HTTP_200_OK
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

    resultado = PosOperationResponse(
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
