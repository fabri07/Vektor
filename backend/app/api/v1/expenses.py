"""Expense entry endpoints."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_active_subscription,
    require_capital_role,
    require_capital_write,
    require_modify_access,
    require_role,
)
from app.application.services import owner_capital_service, tenant_categories_service
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.domain.business_time import fecha_de_negocio
from app.domain.expense_categories import infer_expense_type, resolve_tax_kind
from app.domain.linea_de_compra import (
    CLAVE_GRUPO_DE_COMPRA,
    CODIGO_LINEA_DE_COMPRA,
    MENSAJE_LINEA_DE_COMPRA,
    es_linea_de_compra_el_gasto,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import KIND_RETIRO
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.user import User
from app.persistence.repositories.supplier_repository import SupplierRepository
from app.persistence.repositories.transaction_repository import ExpenseRepository
from app.schemas.common import MessageResponse
from app.schemas.transaction import (
    MARCA_RETIRO,
    CreateExpenseRequest,
    DateRangeResponse,
    ExpenseEntryResponse,
    ExpenseSummaryResponse,
    ProfitWithdrawalRequest,
    UpdateExpenseRequest,
)

router = APIRouter()

VOID_REASON_MANUAL = "MANUAL_ADMIN_VOID"


def _apply_category_label(
    custom_fields: dict[str, object] | None,
    category: str | None,
    label: str | None,
) -> dict[str, object]:
    """FASE 3.1: guarda/limpia el label de categoría "Otro" en custom_fields.

    Si category == OTHER y hay label (trim, ≤50) → custom_fields["category_label"].
    Si la categoría no es OTHER → se quita el label (evita labels huérfanos).
    """
    cf: dict[str, object] = dict(custom_fields or {})
    if category == "OTHER":
        if label and label.strip():
            cf["category_label"] = label.strip()[:50]
    else:
        cf.pop("category_label", None)
    return cf


def _conservar_marca_de_retiro(
    nuevos: dict[str, Any], previos: dict[str, Any] | None
) -> dict[str, Any]:
    """El PATCH reemplaza el dict entero, pero la marca de retiro queda como estaba.

    El editor de campos manda el dict completo: un retiro viejo vuelve con su
    marca y pasa. Agregarla o cambiarla es convertir un gasto en retiro: 422.
    """
    cf = dict(nuevos)
    previos = previos or {}
    if MARCA_RETIRO in cf and cf[MARCA_RETIRO] != previos.get(MARCA_RETIRO):
        raise HTTPException(
            status_code=422,
            detail=f"custom_fields.{MARCA_RETIRO} es reservado: un retiro se carga en capital.",
        )
    if MARCA_RETIRO in previos:
        cf[MARCA_RETIRO] = previos[MARCA_RETIRO]
    return cf


def _sin_grupo_de_compra(custom_fields: dict[str, Any] | None) -> dict[str, Any] | None:
    """T3 · B-3: el grupo de compra lo escribe sólo `registrar_compra`.

    Si el cliente lo pudiera mandar, convertiría cualquier gasto en una línea de
    compra inanulable. Se descarta del body en POST y PATCH.
    """
    if custom_fields is None:
        return None
    return {k: v for k, v in custom_fields.items() if k != CLAVE_GRUPO_DE_COMPRA}


def _expense_snapshot(entry: ExpenseEntry) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "amount": str(entry.amount),
        "category": entry.category,
        "tax_kind": entry.tax_kind,
        "transaction_date": str(entry.transaction_date),
        "description": entry.description,
        "is_recurring": entry.is_recurring,
        "payment_method": entry.payment_method,
        "supplier_name": entry.supplier_name,
        "notes": entry.notes,
        "custom_fields": entry.custom_fields,
        "voided_at": entry.voided_at.isoformat() if entry.voided_at else None,
        "void_reason": entry.void_reason,
    }


def _audit_data_change(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    decision_type: str,
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "record_type": "expense",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "expenses"},
            created_at=datetime.now(UTC),
        )
    )


@router.get(
    "/summary",
    response_model=ExpenseSummaryResponse,
    summary="Last-30-day expense summary",
)
async def expenses_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseSummaryResponse:
    repo = ExpenseRepository(session)
    to_date = date.today()
    from_date = to_date - timedelta(days=30)
    total = await repo.total_expenses(tenant.tenant_id, from_date=from_date, to_date=to_date)
    count = await repo.count_by_date_range(tenant.tenant_id, from_date=from_date, to_date=to_date)
    return ExpenseSummaryResponse(
        total_ars=Decimal(str(total)),
        entry_count=count,
        period_covered=f"{from_date} al {to_date}",
    )


@router.get(
    "/date-range",
    response_model=DateRangeResponse,
    summary="First/last expense date for the tenant",
)
async def expenses_date_range(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> DateRangeResponse:
    repo = ExpenseRepository(session)
    min_date, max_date = await repo.date_range(tenant.tenant_id)
    return DateRangeResponse(min_date=min_date, max_date=max_date)


@router.get("", response_model=list[ExpenseEntryResponse], summary="List expense entries")
async def list_expenses(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    category: str | None = Query(default=None),
    expense_type: str | None = Query(default=None, pattern=r"^(OPEX|COGS)$"),
    supplier_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExpenseEntry]:
    if from_date is None:
        from_date = date.today() - timedelta(days=30)
    repo = ExpenseRepository(session)
    return await repo.list_by_tenant(
        tenant.tenant_id,
        from_date=from_date,
        to_date=to_date,
        category=category,
        expense_type=expense_type,
        supplier_id=supplier_id,
        limit=limit,
        offset=offset,
    )


def _rechazar_mercaderia_sin_articulos(
    category: str | None, expense_type: str | None = None
) -> None:
    """B·5b (decisión del dueño, 2026-10-20): una compra de mercadería que no dice
    qué artículos la componen no es un dato útil. Se carga en «Registrar compra»
    (`POST /purchases/manual`), con productos y cantidades."""
    # Mercadería por categoría o por tipo: un gasto COGS a mano también es una
    # compra de mercadería sin artículos (la otra puerta que dejaba abierta).
    if category == "INVENTORY" or expense_type == "COGS":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "PURCHASE_NEEDS_ITEMS",
                "message": (
                    "Una compra de mercadería se carga en «Registrar compra», "
                    "diciendo qué artículos compraste y cuántos."
                ),
            },
        )


def _conflicto_linea_de_compra() -> HTTPException:
    """T3 · B-3: 409 ``PURCHASE_LINE`` (ver ``domain/linea_de_compra.py``)."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": CODIGO_LINEA_DE_COMPRA, "message": MENSAJE_LINEA_DE_COMPRA},
    )


def _al_minuto(momento: datetime) -> datetime:
    # El editor manda la fecha de un `datetime-local` (minutos, sin segundos) y la
    # base guarda segundos y microsegundos: comparar al minuto, o reabrir el modal
    # y guardar sólo la descripción parecería un cambio de fecha.
    return momento.replace(second=0, microsecond=0, tzinfo=None)


def _cambia_lo_de_la_compra(body: UpdateExpenseRequest, entry: ExpenseEntry) -> bool:
    """T3 · B-3: ¿el PATCH toca lo que la compra comparte con su movimiento?

    Compara VALORES, no presencia: la pantalla manda siempre monto, categoría,
    fecha y proveedor, aunque sólo se haya editado la descripción. El proveedor
    también: el movimiento conserva el suyo (y `GET /suppliers/{id}/products` lee
    de ahí).
    """
    if "supplier_id" in body.model_fields_set and body.supplier_id != entry.supplier_id:
        return True
    if body.supplier_name is not None and body.supplier_name.strip() != (
        entry.supplier_name or ""
    ).strip():
        return True
    if body.amount is not None and Decimal(body.amount) != Decimal(entry.amount):
        return True
    if body.category is not None and body.category != entry.category:
        return True
    if body.expense_type is not None and body.expense_type != entry.expense_type:
        return True
    return body.expense_date is not None and _al_minuto(body.expense_date) != _al_minuto(
        entry.transaction_date
    )


@router.post(
    "",
    response_model=ExpenseEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new expense",
)
async def create_expense(
    body: CreateExpenseRequest,
    background_tasks: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    # F3 review final: la auth (rol) va ANTES que el guard 423 — mismo orden que
    # products/others/suppliers (ver create_product).
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    _sub_guard: None = Depends(require_active_subscription),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExpenseEntry:
    _rechazar_mercaderia_sin_articulos(body.category, body.expense_type)
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session), por lo que si
    # la creación falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_EXPENSE"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    # El supplier_id debe pertenecer al tenant (la FK sola no lo garantiza).
    # Si hay proveedor y no se dio supplier_name, lo denormalizamos desde la
    # entidad para que rankings/análisis (que agrupan por supplier_name) lo vean.
    supplier_name = body.supplier_name
    if body.supplier_id is not None:
        supplier = await SupplierRepository(session).get_by_id(body.supplier_id, tenant.tenant_id)
        if supplier is None:
            raise HTTPException(status_code=400, detail="Supplier not found for this tenant.")
        if not (supplier_name and supplier_name.strip()):
            supplier_name = supplier.name
    repo = ExpenseRepository(session)
    # FASE 3.1: categoría "Otro" editable — el label libre va a custom_fields;
    # category sigue siendo OTHER (reportes/agregaciones intactos).
    custom_fields = _apply_category_label(
        _sin_grupo_de_compra(body.custom_fields), body.category, body.category_label
    )
    # Categoría "Otros" con label → persistir por tenant para que reaparezca en
    # futuros desplegables (sin tabla ni pantalla nueva; vive en custom_fields).
    if body.category == "OTHER" and body.category_label and body.category_label.strip():
        await tenant_categories_service.add_expense_category(
            session, tenant.tenant_id, body.category_label
        )
    entry = ExpenseEntry(
        tenant_id=tenant.tenant_id,
        amount=body.amount,
        category=body.category,
        # Discriminador canónico COGS/OPEX: un valor explícito del usuario gana; si no
        # eligió (None), infiere por categoría (mercadería/INVENTORY → COGS). Antes se
        # persistía el default "OPEX" y una compra de mercadería manual quedaba mal.
        expense_type=infer_expense_type(body.category, explicit=body.expense_type),
        # M·3-3: el que eligió el usuario; «Sin especificar» = sin tipo (la
        # categoría del formulario ya es el código, no hay texto que inferir).
        tax_kind=resolve_tax_kind(body.category, explicit=body.tax_kind),
        transaction_date=body.expense_date,
        description=body.description,
        is_recurring=body.is_recurring,
        payment_method=body.payment_method,
        supplier_name=supplier_name,
        supplier_id=body.supplier_id,
        notes=body.notes,
        custom_fields=custom_fields,
    )
    saved = await repo.save(entry)
    # E0-B-1: después del commit de `get_db_session`, no antes (ver el helper).
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "expense_entry_created", background=background_tasks
    )
    return saved


@router.post(
    "/profit-withdrawal",
    response_model=ExpenseEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an early profit withdrawal as an owner capital movement (retiro)",
    # R-7: el rol primero (403), después el gate (402) y recién ahí el PIN
    # (428, en `require_capital_write`), como en ``POST /capital-movements``.
    dependencies=[Depends(require_capital_role)],
)
async def create_profit_withdrawal(
    body: ProfitWithdrawalRequest,
    background_tasks: BackgroundTasks,
    # Es el mismo alta que ``POST /capital-movements``: el mismo gate.
    _sub_guard: None = Depends(require_active_subscription),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_capital_write),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExpenseEntryResponse:
    """Retiro de ganancias anticipadas (D·2): ya NO es un gasto PAYROLL.

    Repartir ganancia no es un costo del negocio. Escribe un
    ``owner_capital_movements`` tipo ``retiro`` en dinero, y devuelve la MISMA
    forma que antes (``ExpenseEntryResponse``) para que un frontend viejo —el
    deploy es en paralelo— no se rompa. El ``id`` de la respuesta es ahora el
    del movimiento de capital, no el de un gasto. El monto lo fija el usuario
    (no se calcula). Protegido por PIN y, como escribe capital, con la misma
    compuerta que ``/capital-movements`` (OWNER/ADMIN; VIEWER nunca).
    """
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_PROFIT_WITHDRAWAL"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    mov = await owner_capital_service.registrar_movimiento(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        kind=KIND_RETIRO,
        amount=body.amount,
        occurred_on=fecha_de_negocio(body.withdrawal_date),
        payment_method=body.payment_method,
        notes=body.notes,
        endpoint="expenses/profit-withdrawal",
    )
    trigger_score_recalculation_after_commit(
        session,
        str(tenant.tenant_id),
        "profit_withdrawal_created",
        background=background_tasks,
    )
    return ExpenseEntryResponse(
        id=mov.id,
        tenant_id=tenant.tenant_id,
        amount=mov.amount_ars,
        category="PAYROLL",
        expense_type="OPEX",
        transaction_date=body.withdrawal_date,
        description="Retiro de ganancias anticipadas",
        is_recurring=False,
        payment_method=body.payment_method,
        supplier_name=None,
        notes=body.notes,
        custom_fields={MARCA_RETIRO: True},
        created_at=mov.created_at,
    )


@router.get(
    "/custom-categories",
    summary="Categorías de gasto personalizadas del tenant",
)
async def list_custom_expense_categories(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[str]:
    return await tenant_categories_service.list_expense_categories(session, tenant.tenant_id)


@router.get(
    "/count",
    summary="Total de gastos del tenant (para paginación) — DEBE ir antes de /{expense_id}",
)
async def count_expenses(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    category: str | None = Query(default=None),
    expense_type: str | None = Query(default=None, pattern=r"^(OPEX|COGS)$"),
    supplier_id: UUID | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    """Corrección C4 (revisión externa 2026-08-19): `GET /expenses` pagina de
    a 50 (max 200) pero no había forma de saber cuántas páginas hay en total.
    Mismo default de `from_date` que el list (últimos 30 días) para que el
    total coincida con lo que se ve sin filtros explícitos."""
    if from_date is None:
        from_date = date.today() - timedelta(days=30)
    repo = ExpenseRepository(session)
    total = await repo.count_by_tenant(
        tenant.tenant_id,
        from_date=from_date,
        to_date=to_date,
        category=category,
        expense_type=expense_type,
        supplier_id=supplier_id,
    )
    return {"total": total}


@router.get("/{expense_id}", response_model=ExpenseEntryResponse, summary="Get expense by ID")
async def get_expense(
    expense_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    return entry


@router.patch("/{expense_id}", response_model=ExpenseEntryResponse, summary="Update an expense")
async def update_expense(
    expense_id: UUID,
    body: UpdateExpenseRequest,
    background_tasks: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    es_de_compra = es_linea_de_compra_el_gasto(entry)
    if es_de_compra and _cambia_lo_de_la_compra(body, entry):
        # T3 · B-3: el movimiento `purchase` no se entera; se corrige el stock
        # desde el producto (la anulación de la compra entera todavía no existe).
        raise _conflicto_linea_de_compra()
    before = _expense_snapshot(entry)
    if body.category is not None and body.category != entry.category:
        # B·5b: un gasto no se convierte en una compra de mercadería sin artículos.
        # Uno que YA es mercadería se puede editar (monto, fecha, descripción).
        _rechazar_mercaderia_sin_articulos(body.category)
    if body.expense_type == "COGS" and entry.expense_type != "COGS":
        _rechazar_mercaderia_sin_articulos(None, body.expense_type)
    if body.amount is not None:
        entry.amount = body.amount
    if body.category is not None:
        if body.category != entry.category and body.expense_type is None:
            # El discriminador sigue a la categoría: antes, sacar un gasto de
            # mercadería lo dejaba COGS (y ponerlo, OPEX).
            entry.expense_type = infer_expense_type(body.category, product_id=entry.product_id)
        entry.category = body.category
    if body.expense_type is not None:
        entry.expense_type = body.expense_type
    # M·3-3: el tipo sólo vive en TAXES. Mandarlo con otra categoría es un error;
    # sacar el gasto de TAXES lo limpia; null explícito también.
    if body.tax_kind is not None and entry.category != "TAXES":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tax_kind sólo aplica a la categoría TAXES.",
        )
    if "tax_kind" in body.model_fields_set:
        entry.tax_kind = body.tax_kind
    elif body.category is not None or entry.category != "TAXES":
        # Cambiar la categoría sin mandar el tipo lo limpia: un tipo que quedó de
        # antes no revive al volver a TAXES.
        entry.tax_kind = None
    if body.expense_date is not None and not es_de_compra:
        entry.transaction_date = body.expense_date
    # Una línea de compra llega acá sólo con la misma fecha al minuto: se queda la
    # guardada (con sus segundos), igual a la del movimiento.
    if body.description is not None:
        entry.description = body.description
    if body.is_recurring is not None:
        entry.is_recurring = body.is_recurring
    if body.supplier_name is not None:
        entry.supplier_name = body.supplier_name
    # supplier_id: usar model_fields_set para poder limpiarlo (null) explícitamente.
    if "supplier_id" in body.model_fields_set:
        if body.supplier_id is not None:
            supplier = await SupplierRepository(session).get_by_id(
                body.supplier_id, tenant.tenant_id
            )
            if supplier is None:
                raise HTTPException(status_code=400, detail="Supplier not found for this tenant.")
            entry.supplier_id = body.supplier_id
            # Denormalizar nombre si quedó vacío (mantiene análisis por supplier_name).
            if not (entry.supplier_name and entry.supplier_name.strip()):
                entry.supplier_name = supplier.name
        else:
            entry.supplier_id = None
    if body.notes is not None:
        entry.notes = body.notes
    if body.custom_fields is not None:
        previos = entry.custom_fields or {}
        entry.custom_fields = _conservar_marca_de_retiro(
            _sin_grupo_de_compra(body.custom_fields) or {}, entry.custom_fields
        )
        if previos.get(CLAVE_GRUPO_DE_COMPRA):
            # T3 · B-3: el grupo no se agrega ni se pierde con el editor de
            # campos (el envío sólo se reconoce por él).
            entry.custom_fields = {
                **entry.custom_fields,
                CLAVE_GRUPO_DE_COMPRA: previos[CLAVE_GRUPO_DE_COMPRA],
            }
    # FASE 3.1: re-aplicar label de "Otro" si cambió la categoría o el label.
    if body.category_label is not None or body.category is not None:
        entry.custom_fields = _apply_category_label(
            entry.custom_fields, entry.category, body.category_label
        )
    # Relectura de archivos: si esta fila vino de un import, marcarla como editada
    # a mano para que la re-importación no la pise.
    if entry.source_upload_id is not None:
        entry.has_user_edits = True
    saved = await repo.save(entry)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_expense_snapshot(saved),
    )
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "expense_entry_updated", background=background_tasks
    )
    return saved


@router.delete("/{expense_id}", response_model=MessageResponse, summary="Delete an expense entry")
async def delete_expense(
    expense_id: UUID,
    background_tasks: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    if es_linea_de_compra_el_gasto(entry):
        # T3 · B-3: anular el gasto suelto dejaba vivo el movimiento `purchase`
        # (stock, costo y respaldo de ventas como si la compra existiera).
        raise _conflicto_linea_de_compra()
    before = _expense_snapshot(entry)
    entry.voided_at = datetime.now(UTC)
    entry.void_reason = VOID_REASON_MANUAL
    await repo.save(entry)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_expense_snapshot(entry),
    )
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "expense_entry_deleted", background=background_tasks
    )
    return MessageResponse(message="Expense entry voided.")
