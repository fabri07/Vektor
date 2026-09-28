"""Caja (POS): la operación como unidad con identidad propia.

Se apoya en lo que ya existe —bloqueo de filas por producto, validación de
stock, descuento idempotente por línea, idempotencia recuperable de B2— y
agrega lo que faltaba: una cabecera referenciable, los pagos colgando de ella y
un descuento que cierra al centavo.

**El cliente declara el precio; el servidor lo firma.** Hasta B7 el precio salía
del catálogo *al recibir* la operación, y eso rompía la venta cobrada durante un
corte: si el dueño cambiaba el precio en el medio, los pagos dejaban de sumar el
total y la venta —ya cobrada— se rechazaba. Ahora la caja manda el precio y el
factor que cobró junto con el token que el propio servidor emitió en el catálogo
(``domain/pos_price_token``): si la firma coincide, son valores que este servidor
emitió y se usan; si no, se rechaza. Sin token se sigue usando el catálogo.

Lo que el cliente **sigue sin poder** declarar es el importe de la línea ni el
total: los recalcula ``calcular_operacion``, reparto de centavos incluido, y
``Σ tenders == Σ line_total`` sigue siendo la validación.
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
    require_modify_access,
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
from app.application.services import pos_discrepancy_service, stock_service
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
from app.domain.pos_price_token import ResultadoToken
from app.domain.pos_price_token import verificar as verificar_precio
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
from app.persistence.models.pos_stock_discrepancy import (
    ESTADO_ABIERTA,
    RESOLUCION_AJUSTE,
    PosStockDiscrepancy,
)
from app.persistence.models.pos_terminal import PosTerminal
from app.schemas.pos import (
    PosCatalogResponse,
    PosCustomerResponse,
    PosOperationRequest,
    PosOperationResponse,
    PosOperationSummary,
    PosReceiptLine,
    PosReceiptResponse,
    PosReceiptTender,
    PosResolveDiscrepancyRequest,
    PosStockDiscrepancyResponse,
    PosTerminalCreateRequest,
    PosTerminalEnrolledResponse,
    PosTerminalResponse,
    PosVoidRequest,
    a_pos_product,
)

router = APIRouter()

_ACCION_IDEMPOTENTE = "IDEMPOTENT_POST_POS_OPERATION"

#: Lo manda SÓLO el motor de sincronización de la caja, nunca el primer envío:
#: dice "esta venta ya se cobró y el cliente se fue". Va en header y no en el
#: cuerpo porque la huella de idempotencia es sobre el cuerpo validado, y
#: cambiarlo en un reintento daría `IDEMPOTENCY_KEY_REUSED` con la venta
#: existiendo — mismo criterio que `X-POS-Terminal` en B6.
#:
#: **No prueba nada.** Del lado del servidor no existe forma de verificar que el
#: efectivo entró. Lo que hace es dejar la afirmación registrada (`charged_at`,
#: el usuario, la caja) para poder auditarla.
OFFLINE_HEADER = "X-POS-Offline"

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
    offline: str | None = Header(default=None, alias=OFFLINE_HEADER),
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
        if offline:
            # `X-POS-Offline` relaja el invariante "stock negativo prohibido"
            # para ESTA venta, así que no alcanza con el header: hace falta la
            # afirmación fechada de que el pago se recibió y una caja que el
            # dueño habilitó. Nada de esto PRUEBA que el efectivo entró —del
            # lado del servidor no existe tal prueba—, pero deja la afirmación
            # atribuida a alguien, a una PC y a un momento.
            if body.charged_at is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "OFFLINE_WITHOUT_CHARGED_AT",
                        "message": (
                            "Una venta cobrada sin conexión tiene que decir cuándo se cobró."
                        ),
                    },
                )
            if terminal is None:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "TERMINAL_REQUIRED_FOR_CAPTURE",
                        "message": (
                            "Una venta cobrada sin conexión sólo se acepta desde una caja "
                            "habilitada."
                        ),
                    },
                )

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
    #: product_id → (precio, factor) del CATÁLOGO, sólo si difieren de lo cobrado.
    divergencias: dict[uuid.UUID, tuple[Decimal, int]] = {}
    #: product_id → stock disponible, de las líneas cuya salida queda pendiente.
    faltantes: dict[uuid.UUID, int] = {}
    #: Lo que el ticket va a decir de cada producto, congelado al persistir.
    #: El factor es el que se USÓ para cobrar, no el del catálogo: es el que
    #: hace que la cantidad impresa explique el importe impreso.
    snapshots: dict[uuid.UUID, tuple[str, str, int]] = {}
    for item in body.items:
        producto = por_id.get(item.product_id)
        if producto is None:
            raise HTTPException(
                status_code=400, detail=f"Producto {item.product_id} no encontrado."
            )
        factor_catalogo = producto.base_units_per_sale_unit or 1

        if item.unit_price_ars is not None and item.base_units_per_sale_unit is not None:
            # El schema garantiza que los tres campos vienen juntos.
            verificacion = verificar_precio(
                item.price_token,
                tenant.tenant_id,
                producto.id,
                item.unit_price_ars,
                producto.sale_unit,
                item.base_units_per_sale_unit,
            )
            if verificacion is ResultadoToken.VENCIDO:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "PRICE_TOKEN_EXPIRED",
                        "product_id": str(producto.id),
                        "message": (
                            f"El precio con el que se cobró «{producto.name}» es demasiado "
                            "viejo para aceptarlo sin revisar."
                        ),
                    },
                )
            if verificacion is not ResultadoToken.VALIDO:
                # La firma no corresponde a esos valores: este servidor no los
                # emitió. Puede ser una cola editada a mano o un token de otro
                # tenant. No se cae al catálogo en silencio — eso guardaría un
                # precio distinto del que la caja cobró sin que nadie se entere.
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "PRICE_TOKEN_INVALID",
                        "product_id": str(producto.id),
                        "message": (
                            f"No se puede verificar el precio con el que se cobró "
                            f"«{producto.name}»."
                        ),
                    },
                )
            precio = item.unit_price_ars
            factor = item.base_units_per_sale_unit
            if precio != producto.sale_price_ars or factor != factor_catalogo:
                divergencias[producto.id] = (producto.sale_price_ars, factor_catalogo)
        else:
            # Sin precio declarado: catálogo, como antes de B7. Un producto sin
            # precio (los que crea una compra importada nacen con
            # `sale_price_ars = 0` y `requires_completion`) no se puede cobrar:
            # se regalaría y se descontaría su stock.
            if producto.sale_price_ars <= 0:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "PRODUCT_WITHOUT_PRICE",
                        "product_id": str(producto.id),
                        "message": f"«{producto.name}» no tiene precio de venta cargado.",
                    },
                )
            precio = producto.sale_price_ars
            factor = factor_catalogo

        snapshots[producto.id] = (producto.name, producto.sale_unit, factor)

        if producto.stock_units < item.quantity:
            if not offline:
                raise stock_service.InsufficientStockError(
                    item.product_id, producto.stock_units, item.quantity, producto.name
                )
            # La venta ya se cobró: rechazarla no devuelve la mercadería ni la
            # plata. Entra completa y queda una salida pendiente (B11). Se
            # decide acá, con la fila del producto bloqueada, y el bucle de
            # persistencia sólo obedece: si volviera a preguntar "¿alcanza?"
            # habría dos lugares decidiendo lo mismo.
            faltantes[item.product_id] = producto.stock_units
        pedidas.append(
            LineaPedida(
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price_list=precio,
                discount_ars=item.discount_ars,
                # El precio es por unidad de venta; la cantidad, en unidades base.
                base_units_per_sale_unit=factor,
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
        captured_offline=bool(offline),
        charged_at=body.charged_at,
        client_session_id=body.client_session_id,
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
        if linea.product_id not in faltantes:
            await stock_service.decrement_for_sale(venta, session)

        nombre, unidad, factor_snap = snapshots[linea.product_id]
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
                unit_price_catalog_ars=(
                    divergencias[linea.product_id][0]
                    if linea.product_id in divergencias
                    else None
                ),
                # El ticket deja de depender del catálogo de mañana.
                product_name_snapshot=nombre,
                sale_unit_snapshot=unidad,
                base_units_snapshot=factor_snap,
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

    # Las salidas pendientes se registran después del flush: la discrepancia
    # apunta a la línea, y la línea recién ahí tiene id.
    for ln in lineas:
        if ln.product_id in faltantes:
            pos_discrepancy_service.registrar(
                session,
                tenant_id=tenant.tenant_id,
                operation_id=operacion.id,
                line_id=ln.id,
                sale_entry_id=ln.sale_entry_id,
                product_id=ln.product_id,
                pending_units=ln.quantity,
                stock_at_capture=faltantes[ln.product_id],
            )
            session.add(
                DecisionAuditLog(
                    tenant_id=tenant.tenant_id,
                    decision_type="POS_STOCK_CAPTURED",
                    decision_data={
                        "operation_id": str(operacion.id),
                        "client_operation_id": operacion.client_operation_id,
                        "product_id": str(ln.product_id),
                        "pending_units": ln.quantity,
                        "stock_at_capture": faltantes[ln.product_id],
                        "charged_at": (
                            operacion.charged_at.isoformat()
                            if operacion.charged_at
                            else None
                        ),
                        "terminal_id": (
                            str(operacion.terminal_id) if operacion.terminal_id else None
                        ),
                    },
                    triggered_by="ui:pos",
                    actor_user_id=user.user_id,
                    context={"endpoint": "pos.create_operation"},
                    created_at=datetime.now(UTC),
                )
            )

    # Una divergencia de precio no es un error: es lo normal cuando la venta se
    # cobró antes de que el dueño cambiara el precio. Pero tiene que quedar
    # visible con los dos números, porque es la única forma de revisar después
    # si la caja cobró lo que correspondía.
    if divergencias:
        session.add(
            DecisionAuditLog(
                tenant_id=tenant.tenant_id,
                decision_type="POS_PRICE_DIVERGENCE",
                decision_data={
                    "operation_id": str(operacion.id),
                    "client_operation_id": operacion.client_operation_id,
                    "captured_offline": operacion.captured_offline,
                    "charged_at": (
                        operacion.charged_at.isoformat() if operacion.charged_at else None
                    ),
                    "lines": [
                        {
                            "product_id": str(ln.product_id),
                            "cobrado_ars": str(ln.unit_price_list),
                            "catalogo_ars": str(divergencias[ln.product_id][0]),
                            "factor_cobrado": ln.base_units_snapshot,
                            "factor_catalogo": divergencias[ln.product_id][1],
                        }
                        for ln in lineas
                        if ln.product_id in divergencias
                    ],
                },
                triggered_by="ui:pos",
                actor_user_id=user.user_id,
                context={"endpoint": "pos.create_operation"},
                created_at=datetime.now(UTC),
            )
        )

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


async def _ejecutar_anulacion(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user: User,
    operacion: PosOperation,
    lineas: list[PosOperationLine],
    tenders: list[PosTender],
    reason: str | None,
    background: BackgroundTasks,
) -> None:
    """Anula el ticket entero: ventas, stock y deudas de stock. Idempotente.

    Vive acá y no dentro del handler porque la resolución de una salida
    pendiente (B11) tiene que anular EXACTAMENTE igual: dos implementaciones de
    "anular un ticket" se separan, y una de ellas se olvidaría de revertir algo.
    """
    if operacion.status == "VOIDED":
        return

    ahora = datetime.now(UTC)
    ventas_anuladas: list[str] = []
    ids_de_venta = [ln.sale_entry_id for ln in lineas if ln.sale_entry_id is not None]
    ventas = (
        (
            await session.execute(
                select(SaleEntry)
                .where(
                    SaleEntry.id.in_(ids_de_venta),
                    SaleEntry.tenant_id == tenant_id,
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
        await stock_service.revert_sale_stock(venta.id, tenant_id, session)
        # Mismo registro que una anulación suelta, para que el historial de la
        # venta cuente lo mismo venga de donde venga.
        _audit_data_change(
            session,
            tenant_id=tenant_id,
            user_id=user.user_id,
            decision_type="DATA_RECORD_VOIDED",
            before=antes,
            after=_sale_snapshot(venta),
        )
        ventas_anuladas.append(str(venta.id))

    operacion.status = "VOIDED"
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
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
                "reason": reason,
                "source": "pos",
            },
            triggered_by="ui:pos",
            actor_user_id=user.user_id,
            context={"endpoint": "pos.void"},
            created_at=ahora,
        )
    )
    await session.flush()

    await pos_discrepancy_service.cerrar_por_anulacion(
        session, tenant_id, operacion.id, user.user_id
    )

    trigger_score_recalculation_after_commit(
        session, str(tenant_id), "pos_operation_voided", background
    )


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

    await _ejecutar_anulacion(
        session,
        tenant_id=tenant.tenant_id,
        user=user,
        operacion=operacion,
        lineas=lineas,
        tenders=tenders,
        reason=body.reason if body is not None else None,
        background=background,
    )
    return _a_respuesta(operacion, lineas, tenders)


def _a_discrepancia(
    discrepancia: PosStockDiscrepancy, product_name: str | None, operation_date: datetime
) -> PosStockDiscrepancyResponse:
    return PosStockDiscrepancyResponse(
        id=discrepancia.id,
        operation_id=discrepancia.operation_id,
        product_id=discrepancia.product_id,
        product_name=product_name,
        pending_units=discrepancia.pending_units,
        stock_at_capture=discrepancia.stock_at_capture,
        operation_date=operation_date,
        status=discrepancia.status,
        resolution=discrepancia.resolution,
        reason=discrepancia.reason,
        resolved_at=discrepancia.resolved_at,
        created_at=discrepancia.created_at,
    )


@router.get(
    "/stock-discrepancies",
    response_model=list[PosStockDiscrepancyResponse],
    summary="Salidas de stock que quedaron pendientes por una venta de caja",
)
async def list_stock_discrepancies(
    abiertas: bool = Query(default=True, alias="open"),
    limit: int = Query(default=100, ge=1, le=200),
    tenant: Tenant = Depends(get_current_tenant),
    # Sólo lectura: no pide PIN. Resolver sí, porque mueve stock.
    _user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> list[PosStockDiscrepancyResponse]:
    """Lo que la caja vendió y el stock no pudo respaldar, para que alguien decida.

    Fuera de `CASHIER_ALLOWED_ROUTES` a propósito: qué pasó con el stock no es
    una decisión de caja.
    """
    filas = await pos_discrepancy_service.listar(
        session, tenant.tenant_id, solo_abiertas=abiertas, limit=limit
    )
    return [_a_discrepancia(d, nombre, fecha) for d, nombre, fecha in filas]


@router.post(
    "/stock-discrepancies/{discrepancy_id}/resolve",
    response_model=PosStockDiscrepancyResponse,
    summary="Resolver una salida pendiente: la mercadería estaba, o la venta está mal",
)
async def resolve_stock_discrepancy(
    discrepancy_id: uuid.UUID,
    body: PosResolveDiscrepancyRequest,
    background: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    # Mueve stock y puede anular un ticket: OWNER o `can_modify_sensitive`, con PIN.
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> PosStockDiscrepancyResponse:
    """Dos caminos, los dos con efecto exacto sobre el stock y auditados.

    - `ajuste_de_entrada`: entra el faltante —**recalculado ahora**, no el de la
      captura— como ajuste de reconciliación fechado el día de la venta, y
      después sale la venta completa.
    - `anular_ticket`: la venta no existió; se anula el ticket entero (B10).

    No hay una tercera opción que sólo oculte el aviso: ocultar no reconcilia
    nada. Idempotente: resolver dos veces devuelve lo mismo sin repetir efectos.
    """
    await acquire_write_lock_shared(session, tenant.tenant_id)
    discrepancia = await pos_discrepancy_service.obtener(
        session, tenant.tenant_id, discrepancy_id, para_actualizar=True
    )
    if discrepancia is None:
        raise HTTPException(status_code=404, detail="Diferencia no encontrada.")

    cargada = await _cargar_operacion(
        session, tenant.tenant_id, operation_id=discrepancia.operation_id, para_actualizar=True
    )
    if cargada is None:
        raise HTTPException(status_code=404, detail="Operación no encontrada.")
    operacion, lineas, tenders = cargada

    producto = (
        await session.get(Product, discrepancia.product_id)
        if discrepancia.product_id
        else None
    )
    nombre = producto.name if producto is not None else None

    if discrepancia.status != ESTADO_ABIERTA:
        return _a_discrepancia(discrepancia, nombre, operacion.operation_date)

    if body.resolution == RESOLUCION_AJUSTE:
        faltante = await pos_discrepancy_service.aplicar_ajuste_de_entrada(
            session, discrepancia, occurred_at=operacion.operation_date
        )
        pos_discrepancy_service.marcar_resuelta(
            session,
            discrepancia,
            resolucion=body.resolution,
            reason=body.reason,
            user_id=user.user_id,
            detalle={"unidades_ajustadas": faltante},
        )
        trigger_score_recalculation_after_commit(
            session, str(tenant.tenant_id), "pos_stock_discrepancy_resolved", background
        )
    else:
        discrepancia.reason = body.reason
        await _ejecutar_anulacion(
            session,
            tenant_id=tenant.tenant_id,
            user=user,
            operacion=operacion,
            lineas=lineas,
            tenders=tenders,
            reason=body.reason,
            background=background,
        )
        if discrepancia.status == ESTADO_ABIERTA:
            # El ticket ya estaba anulado: `_ejecutar_anulacion` no hace nada y
            # la deuda igual tiene que cerrarse, porque esa venta ya no existe.
            pos_discrepancy_service.marcar_resuelta(
                session,
                discrepancia,
                resolucion=body.resolution,
                reason=body.reason,
                user_id=user.user_id,
            )

    await session.flush()
    return _a_discrepancia(discrepancia, nombre, operacion.operation_date)


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
        # El snapshot manda: un comprobante dice lo que decía cuando se emitió.
        # Renombrar un producto no puede cambiar el ticket de ayer, y borrarlo
        # no puede dejarlo en "Producto eliminado".
        producto = productos.get(ln.product_id) if ln.product_id else None
        nombre = ln.product_name_snapshot
        unidad = ln.sale_unit_snapshot
        factor = ln.base_units_snapshot
        if unidad is None or factor is None:
            # Línea anterior a B7: no hay snapshot y no se inventa. Se cae al
            # catálogo actual, que es exactamente lo que se hacía antes.
            unidad = producto.sale_unit if producto is not None else None
            factor = (producto.base_units_per_sale_unit or 1) if producto is not None else None
        if nombre is None:
            nombre = producto.name if producto is not None else "Producto eliminado"
        cantidad = (
            formatear_cantidad(ln.quantity, unidad, factor)
            if unidad is not None and factor is not None
            else str(ln.quantity)
        )
        return PosReceiptLine(
            position=ln.position,
            product_name=nombre,
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
        items=[a_pos_product(p, tenant.tenant_id) for p in pagina],
        next_cursor=pagina[-1].id if hay_mas and pagina else None,
        business_name=tenant.display_name,
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
