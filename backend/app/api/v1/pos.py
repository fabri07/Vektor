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

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    assert_pos_permission,
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    get_current_user,
    require_active_subscription,
    require_owner_stepup,
    require_role,
    require_void_access,
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
    idempotent_request_exists,
    record_idempotent_response,
)
from app.application.services.maintenance_lock_service import acquire_write_lock_shared
from app.application.services.pos_terminal_service import (
    dar_de_baja,
    exigir_terminal_si_es_cajero,
    habilitar,
    listar,
    resolver_terminal,
)
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.domain.pos_operation import (
    LineaPedida,
    OperacionInvalidaError,
    TendersNoCuadranError,
    calcular_operacion,
    calcular_vuelto,
    validar_tenders,
)
from app.domain.pos_permissions import CASHIER_ROLE, PosPermission
from app.domain.pos_terminal import TERMINAL_HEADER
from app.domain.sale_unit import formatear_cantidad
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
from app.persistence.models.customer import Customer
from app.persistence.models.pos_terminal import PosTerminal
from app.schemas.pos import (
    PosCatalogResponse,
    PosCustomerResponse,
    PosOperationRequest,
    PosOperationResponse,
    PosOperationSummary,
    PosProductResponse,
    PosReceiptLine,
    PosReceiptResponse,
    PosReceiptTender,
    PosTerminalCreateRequest,
    PosTerminalEnrolledResponse,
    PosTerminalResponse,
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
    user: User = Depends(require_role("OWNER", "ADMIN", CASHIER_ROLE)),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    _sub_guard: None = Depends(require_active_subscription),
    session: AsyncSession = Depends(get_db_session),
    terminal_secret: str | None = Header(default=None, alias=TERMINAL_HEADER),
) -> PosOperationResponse:
    """Cobra un carrito: una `SaleEntry` por línea, más la cabecera y sus pagos.

    Atómica: si una línea falla, no queda ni media venta ni medio descuento de
    stock. La clave de idempotencia es el `client_operation_id` del cuerpo —lo
    genera la caja antes del primer envío—, así que un reintento tras una
    respuesta perdida devuelve el ticket original en vez de cobrar de nuevo.
    """
    # Crear y recuperar se autorizan DISTINTO (B5). Una operación que ya existe
    # se devuelve aunque al cajero le hayan revocado el permiso después: la venta
    # se hizo, y negarle el ticket lo deja sin saber si cobró. Sólo una operación
    # NUEVA exige los permisos actuales, y se chequean ANTES del claim para que
    # un 403 no consuma la clave: el mismo pedido, reintentado cuando el dueño
    # habilite el permiso, tiene que poder entrar.
    terminal = None
    if not await idempotent_request_exists(session, tenant.tenant_id, body.client_operation_id):
        # La terminal, como los permisos, se valida sólo para una venta NUEVA: el
        # reintento de una ya hecha devuelve el ticket aunque después hayan dado
        # de baja la PC (B6).
        terminal = await resolver_terminal(session, tenant.tenant_id, terminal_secret)
        exigir_terminal_si_es_cajero(user, terminal)
        if body.discount_ars > 0 or any(item.discount_ars > 0 for item in body.items):
            assert_pos_permission(user, PosPermission.DISCOUNT)
        if any(t.payment_method == FIADO_PAYMENT_METHOD for t in body.tenders):
            assert_pos_permission(user, PosPermission.FIADO)

    # La idempotencia va antes de tocar stock, precios o locks. Un reintento
    # tiene que poder contestar sin repetir nada de eso.
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
                # El precio es por unidad de venta; la cantidad, en unidades base.
                base_units_per_sale_unit=producto.base_units_per_sale_unit or 1,
            )
        )

    # Toda la aritmética en un módulo puro: el reparto de centavos es lo que
    # hace que la suma de las líneas sea exactamente lo que se cobra.
    try:
        calculada = calcular_operacion(pedidas, body.discount_ars)
        try:
            validar_tenders(calculada.total, [t.amount_ars for t in body.tenders])
        except TendersNoCuadranError as exc:
            # El caso típico no es un cliente tramposo sino un precio que el
            # dueño cambió entre que la caja agregó el producto y el cobro. Sin
            # el total y los precios vigentes, la caja sólo tendría un texto y
            # la venta quedaría trabada sin explicación: con ellos actualiza el
            # carrito y le muestra al cajero qué cambió.
            raise HTTPException(
                status_code=422,
                detail={
                    "code": exc.CODE,
                    "message": str(exc),
                    "expected_total_ars": str(calculada.total),
                    "lines": [
                        {
                            "product_id": str(linea.product_id),
                            "unit_price_list": str(linea.unit_price_list),
                        }
                        for linea in calculada.lineas
                    ],
                },
            ) from exc
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
        terminal_id=terminal.id if terminal is not None else None,
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
    # OWNER/ADMIN: `require_modify_access` (PIN), como siempre. CASHIER: permiso
    # `void_ticket` + ventana de SU PIN (ver `require_void_access`).
    user: User = Depends(require_void_access),
    session: AsyncSession = Depends(get_db_session),
    terminal_secret: str | None = Header(default=None, alias=TERMINAL_HEADER),
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
    # Un cajero sólo anula desde una caja habilitada (B6).
    terminal = await resolver_terminal(session, tenant.tenant_id, terminal_secret)
    exigir_terminal_si_es_cajero(user, terminal)

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

    if user.role_code == CASHIER_ROLE:
        # Un cajero anula SUS tickets; los de un compañero los resuelve el
        # encargado. Anular es devolver plata: queda a nombre de quien cobró.
        if operacion.created_by_user_id != user.user_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "VOID_NOT_OWN_TICKET",
                    "message": "Sólo podés anular tus propios tickets. Pedíselo al encargado.",
                },
            )
        if body is None or not (body.reason or "").strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VOID_REASON_REQUIRED",
                    "message": "Indicá por qué se anula el ticket.",
                },
            )

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


def _numero_de_ticket(operation_id: uuid.UUID) -> str:
    """Número corto para ubicar un ticket. NO es correlativo: el ticket es no fiscal."""
    return operation_id.hex[:8].upper()


def _solo_propios_si_es_cajero(user: User) -> uuid.UUID | None:
    """Un cajero ve sólo SUS tickets, como sólo anula los suyos (B10/B5).

    Reimprimir un ticket ajeno no toca plata, pero listar los de los compañeros
    le mostraría cuánto vende cada uno, que no es información de caja.
    """
    return user.user_id if user.role_code == CASHIER_ROLE else None


@router.get(
    "/operations",
    response_model=list[PosOperationSummary],
    summary="Últimos tickets, para reimprimir",
)
async def list_pos_operations(
    limit: int = Query(default=20, ge=1, le=50),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN", CASHIER_ROLE)),
    session: AsyncSession = Depends(get_db_session),
) -> list[PosOperationSummary]:
    stmt = select(PosOperation, User.full_name).outerjoin(
        User, User.user_id == PosOperation.created_by_user_id
    ).where(PosOperation.tenant_id == tenant.tenant_id)
    propio = _solo_propios_si_es_cajero(user)
    if propio is not None:
        stmt = stmt.where(PosOperation.created_by_user_id == propio)
    filas = (
        await session.execute(
            stmt.order_by(PosOperation.created_at.desc(), PosOperation.id.desc()).limit(limit)
        )
    ).all()
    return [
        PosOperationSummary(
            id=op.id,
            number=_numero_de_ticket(op.id),
            operation_date=op.operation_date,
            total_ars=op.total_ars,
            status=op.status,
            cashier_name=nombre,
        )
        for op, nombre in filas
    ]


@router.get(
    "/operations/{operation_id}/receipt",
    response_model=PosReceiptResponse,
    summary="El ticket de una operación, para imprimir o reimprimir",
)
async def pos_operation_receipt(
    operation_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN", CASHIER_ROLE)),
    session: AsyncSession = Depends(get_db_session),
) -> PosReceiptResponse:
    """Sólo lectura: reimprimir no crea nada ni audita un cobro.

    Un ticket anulado también se devuelve (con su `status`): tiene que poder
    reimprimirse como anulado. De otro tenant, o ajeno para un cajero → 404,
    sin revelar que existe.
    """
    cargada = await _cargar_operacion(session, tenant.tenant_id, operation_id=operation_id)
    propio = _solo_propios_si_es_cajero(user)
    if cargada is None or (propio is not None and cargada[0].created_by_user_id != propio):
        raise HTTPException(status_code=404, detail="Operación no encontrada.")
    operacion, lineas, tenders = cargada

    ids = [ln.product_id for ln in lineas if ln.product_id is not None]
    productos = (
        {
            p.id: p
            for p in (
                await session.execute(
                    select(Product).where(
                        Product.tenant_id == tenant.tenant_id, Product.id.in_(ids)
                    )
                )
            ).scalars()
        }
        if ids
        else {}
    )

    def _renglon(ln: PosOperationLine) -> PosReceiptLine:
        producto = productos.get(ln.product_id) if ln.product_id else None
        cantidad = (
            formatear_cantidad(
                ln.quantity, producto.sale_unit, producto.base_units_per_sale_unit or 1
            )
            if producto is not None
            else str(ln.quantity)
        )
        return PosReceiptLine(
            position=ln.position,
            product_name=producto.name if producto is not None else "Producto eliminado",
            quantity=ln.quantity,
            quantity_display=cantidad,
            unit_price_list=ln.unit_price_list,
            gross_ars=ln.line_total_ars + ln.discount_line_ars + ln.discount_global_share_ars,
            discount_line_ars=ln.discount_line_ars,
            line_total_ars=ln.line_total_ars,
        )

    cajero = (
        await session.get(User, operacion.created_by_user_id)
        if operacion.created_by_user_id
        else None
    )
    caja = (
        await session.get(PosTerminal, operacion.terminal_id) if operacion.terminal_id else None
    )
    cliente = (
        await session.get(Customer, operacion.customer_id) if operacion.customer_id else None
    )
    nombre_cliente = None
    # El centinela "Local" no es un cliente: no se imprime.
    if cliente is not None and not cliente.is_sentinel:
        nombre_cliente = " ".join(p for p in (cliente.name, cliente.last_name) if p)

    return PosReceiptResponse(
        id=operacion.id,
        number=_numero_de_ticket(operacion.id),
        business_name=tenant.display_name,
        operation_date=operacion.operation_date,
        status=operacion.status,
        lines=[_renglon(ln) for ln in lineas],
        tenders=[
            PosReceiptTender(payment_method=t.payment_method, amount_ars=t.amount_ars)
            for t in tenders
        ],
        subtotal_ars=operacion.subtotal_ars,
        discount_ars=operacion.discount_ars,
        total_ars=operacion.total_ars,
        cash_received_ars=operacion.cash_received_ars,
        cash_change_ars=operacion.cash_change_ars,
        customer_name=nombre_cliente,
        cashier_name=cajero.full_name if cajero is not None else None,
        terminal_name=caja.name if caja is not None else None,
    )


@router.get(
    "/catalog",
    response_model=PosCatalogResponse,
    summary="Catálogo de caja: productos vendibles, sin costos, paginado por cursor",
)
async def pos_catalog(
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, ge=1, le=500),
    after: uuid.UUID | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> PosCatalogResponse:
    """Productos activos con precio, ordenados por id.

    El orden por id y el cursor `after` hacen que recorrer todas las páginas dé
    el catálogo COMPLETO, sin saltos ni repeticiones aunque cambien nombres o
    precios en el medio: es lo que necesita la caja offline (B7) para bajarse
    todo. Un producto sin precio no aparece: no se puede cobrar.
    """
    stmt = select(Product).where(
        Product.tenant_id == tenant.tenant_id,
        Product.is_active.is_(True),
        Product.sale_price_ars > 0,
    )
    if after is not None:
        stmt = stmt.where(Product.id > after)
    if q and q.strip():
        patron = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(patron),
                Product.sku.ilike(patron),
                Product.barcode.ilike(patron),
                Product.internal_sku.ilike(patron),
            )
        )
    filas = list(
        (await session.execute(stmt.order_by(Product.id).limit(limit + 1))).scalars().all()
    )
    hay_mas = len(filas) > limit
    pagina = filas[:limit]
    return PosCatalogResponse(
        items=[PosProductResponse.model_validate(p) for p in pagina],
        next_cursor=pagina[-1].id if hay_mas and pagina else None,
    )


@router.get(
    "/customers",
    response_model=list[PosCustomerResponse],
    summary="Clientes para fiar: sólo id y nombre",
)
async def pos_customers(
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[PosCustomerResponse]:
    """Para elegir a quién se le fía. Sólo lo necesita quien puede fiar.

    Devuelve id y nombre, nada más: la ficha del cliente (DNI, CUIT, dirección)
    no hace falta para cobrar. El centinela "Local" y los dados de baja no
    aparecen: no se le fía a "Local".
    """
    assert_pos_permission(user, PosPermission.FIADO)
    stmt = select(Customer).where(
        Customer.tenant_id == tenant.tenant_id,
        Customer.deactivated_at.is_(None),
    )
    if q and q.strip():
        patron = f"%{q.strip()}%"
        stmt = stmt.where(or_(Customer.name.ilike(patron), Customer.last_name.ilike(patron)))
    # Se pide de más porque el centinela se filtra en Python (vive en custom_fields).
    filas = (await session.execute(stmt.order_by(Customer.name).limit(limit + 1))).scalars()
    clientes = [c for c in filas if not c.is_sentinel][:limit]
    return [
        PosCustomerResponse(
            id=c.id, name=" ".join(x for x in (c.name, c.last_name) if x)
        )
        for c in clientes
    ]


@router.post(
    "/terminals",
    response_model=PosTerminalEnrolledResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Habilitar esta PC como caja (devuelve el secreto UNA sola vez)",
)
async def enroll_pos_terminal(
    body: PosTerminalCreateRequest,
    user: User = Depends(require_owner_stepup),
    session: AsyncSession = Depends(get_db_session),
) -> PosTerminalEnrolledResponse:
    """Sólo el dueño, con PIN. El secreto no se guarda: si se pierde, se da de
    baja la caja y se habilita otra vez."""
    terminal, secreto = await habilitar(session, user, body.name)
    return PosTerminalEnrolledResponse(
        id=terminal.id,
        name=terminal.name,
        created_at=terminal.created_at,
        last_seen_at=terminal.last_seen_at,
        disabled_at=terminal.disabled_at,
        secret=secreto,
    )


@router.get(
    "/terminals",
    response_model=list[PosTerminalResponse],
    summary="Cajas habilitadas del negocio (sin secretos)",
)
async def list_pos_terminals(
    user: User = Depends(require_owner_stepup),
    session: AsyncSession = Depends(get_db_session),
) -> list[PosTerminal]:
    return await listar(session, user.tenant_id)


@router.post(
    "/terminals/{terminal_id}/disable",
    response_model=PosTerminalResponse,
    summary="Dar de baja una caja (idempotente)",
)
async def disable_pos_terminal(
    terminal_id: uuid.UUID,
    user: User = Depends(require_owner_stepup),
    session: AsyncSession = Depends(get_db_session),
) -> PosTerminal:
    return await dar_de_baja(session, user, terminal_id)
