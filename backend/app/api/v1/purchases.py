"""Compra manual de mercadería (comprobante multi-línea, transaccional).

POST /purchases/manual — por línea: crea/restock producto + stock + gasto COGS,
asociado a un proveedor. Atómico: si una línea falla, no queda nada a medias.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_role,
)
from app.application.services import purchase_service
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.purchase_service import PurchaseError
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.purchase import ManualPurchaseRequest, ManualPurchaseResponse

router = APIRouter()


@router.post(
    "/manual",
    response_model=ManualPurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a manual merchandise purchase (multi-line, atomic)",
)
async def create_manual_purchase(
    body: ManualPurchaseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3 review final: la auth (rol) va ANTES que el guard 423 — mismo orden que
    # products/others/suppliers (ver create_product).
    user: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ManualPurchaseResponse:
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_PURCHASE"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})

    try:
        result = await purchase_service.register_manual_purchase(
            session, tenant.tenant_id, body, user.user_id
        )
    except PurchaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    trigger_score_recalculation.delay(str(tenant.tenant_id), "manual_purchase_created")
    return result
