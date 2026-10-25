"""Capital de los socios (D·2): alta, listado y anulación por API.

Permisos: sólo OWNER y ADMIN, nunca CASHIER ni VIEWER. Escribir (alta y
anulación) además exige lo mismo que el retiro de ganancias de siempre:
``require_modify_access`` (OWNER, o ADMIN con ``can_modify_sensitive``, y la
ventana del PIN). El ``tenant_id`` sale del JWT; un id de otro tenant da 404.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_tenant,
    require_active_subscription,
    require_capital_role,
    require_capital_write,
    require_role,
)
from app.application.services import owner_capital_service as capital
from app.application.services.facts_provider import load_capital_frame
from app.application.services.facts_service import (
    CapitalDeSocio,
    MontoPorForma,
    Period,
    capital_de_socios,
)
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.owner_capital import (
    CapitalAmountsByForm,
    CapitalCashFlow,
    CapitalInitialDeclared,
    CapitalKind,
    CapitalMovementListResponse,
    CapitalMovementResponse,
    CapitalPartnerSummary,
    CapitalSummaryResponse,
    CreateCapitalMovementRequest,
    VoidCapitalMovementRequest,
)

router = APIRouter()

_ROLES = ("OWNER", "ADMIN")


@router.post(
    "",
    response_model=CapitalMovementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar un aporte, retiro o préstamo de socio (en dinero)",
    # R-7: el rol primero (403), después el gate (402) y recién ahí el PIN
    # (428, en `require_capital_write`), igual que el resto de las altas.
    dependencies=[Depends(require_capital_role)],
)
async def create_capital_movement(
    body: CreateCapitalMovementRequest,
    background_tasks: BackgroundTasks,
    _gate: None = Depends(require_active_subscription),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_capital_write),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OwnerCapitalMovement:
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_CAPITAL_MOVEMENT"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    mov = await capital.registrar_movimiento(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        kind=body.kind,
        amount=body.amount,
        occurred_on=body.occurred_on,
        payment_method=body.payment_method,
        partner_name=body.partner_name,
        notes=body.notes,
    )
    # Después del commit: el worker lee con su propia sesión, y encolarlo antes
    # lo haría recalcular sin este movimiento (o con uno que un rollback descarta).
    trigger_score_recalculation_after_commit(
        session,
        str(tenant.tenant_id),
        "owner_capital_movement_created",
        background=background_tasks,
    )
    return mov


@router.get(
    "",
    response_model=CapitalMovementListResponse,
    summary="Listar movimientos de capital de los socios",
)
async def list_capital_movements(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    kind: CapitalKind | None = Query(default=None),
    include_voided: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
    tenant: Tenant = Depends(get_current_tenant),
    _user: User = Depends(require_role(*_ROLES)),
    session: AsyncSession = Depends(get_db_session),
) -> CapitalMovementListResponse:
    items = await capital.listar_movimientos(
        session,
        tenant_id=tenant.tenant_id,
        from_date=from_date,
        to_date=to_date,
        kind=kind,
        include_voided=include_voided,
        limit=limit,
    )
    return CapitalMovementListResponse(
        items=[CapitalMovementResponse.model_validate(m) for m in items]
    )


def _por_forma(m: MontoPorForma) -> CapitalAmountsByForm:
    return CapitalAmountsByForm(dinero=m.dinero, especie=m.especie)


def _socio(c: CapitalDeSocio) -> CapitalPartnerSummary:
    return CapitalPartnerSummary(
        partner_name=c.socio,
        aportes=_por_forma(c.aportes),
        retiros=_por_forma(c.retiros),
        prestamos_recibidos=_por_forma(c.prestamos_recibidos),
        devoluciones=_por_forma(c.devoluciones),
        prestamo_pendiente=c.prestamo_pendiente,
        prestamo_pendiente_inconsistente=c.prestamo_pendiente_inconsistente,
    )


@router.get(
    "/summary",
    response_model=CapitalSummaryResponse,
    summary="Capital por socio del período y préstamos pendientes (D·4)",
)
async def capital_summary(
    from_date: date = Query(...),
    to_date: date = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    _user: User = Depends(require_role(*_ROLES)),
    session: AsyncSession = Depends(get_db_session),
) -> CapitalSummaryResponse:
    """Aportes, retiros y préstamos por socio. No es parte del resultado ni de la caja."""
    if from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from_date must be <= to_date.",
        )
    frame = await load_capital_frame(session, tenant.tenant_id, to_date)
    r = capital_de_socios(frame, Period(start=from_date, end=to_date, label="capital"))
    return CapitalSummaryResponse(
        from_date=r.start,
        to_date=r.end,
        has_data=r.has_data,
        has_in_kind=r.hay_especie,
        partners=[_socio(c) for c in r.socios],
        totals=_socio(r.totales) if r.totales is not None else None,
        cash_flow=(
            CapitalCashFlow(
                entradas=r.flujo_dinero.entradas,
                salidas=r.flujo_dinero.salidas,
                neto=r.flujo_dinero.neto,
            )
            if r.flujo_dinero is not None
            else None
        ),
        initial_capital_declared=(
            CapitalInitialDeclared(
                amount_ars=r.capital_inicial_declarado.monto,
                occurred_on=r.capital_inicial_declarado.fecha,
            )
            if r.capital_inicial_declarado is not None
            else None
        ),
    )


@router.post(
    "/{movement_id}/void",
    response_model=CapitalMovementResponse,
    summary="Anular un movimiento de capital (no se borra)",
)
async def void_capital_movement(
    movement_id: UUID,
    body: VoidCapitalMovementRequest,
    background_tasks: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_capital_write),
    session: AsyncSession = Depends(get_db_session),
) -> OwnerCapitalMovement:
    try:
        mov = await capital.anular_movimiento(
            session,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            movement_id=movement_id,
            reason=body.reason,
        )
    except capital.CapitalMovementNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Capital movement not found."
        ) from exc
    except capital.CapitalMovementAlreadyVoidedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CAPITAL_MOVEMENT_ALREADY_VOIDED"},
        ) from exc
    trigger_score_recalculation_after_commit(
        session,
        str(tenant.tenant_id),
        "owner_capital_movement_voided",
        background=background_tasks,
    )
    return mov
