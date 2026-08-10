"""La compuerta del motor de costos de compra se valida en el BACKEND.

Este repo no tiene staging: el rollout por tenant **es** el staging, y el motor
de costos sale con la allowlist vacía. Lo que la compuerta protege es plata —
alcanza con mapear una columna de descuento para que el costo de un producto, y
por lo tanto su margen, quede distinto.

Por eso los dos puntos de control están en la API y no en la pantalla: esconder
el control del frontend no impide que un cliente arme el body a mano, ni que una
pantalla vieja cacheada mande la decisión igual.

1. El preview del reparto (`/purchase-groups`) → 403.
2. El confirm con `purchase_cost_decisions` → 422 explicativo, antes del lease.

El 422 no es un descarte silencioso a propósito: el usuario cree haber resuelto
algo sobre sus costos, y dejarlo confirmar como si nada le daría un import que no
hizo lo que pidió.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.tenant import Tenant

_CTX = "sheet:Compras"

_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
    "comprobante": "invoice_number",
    "proveedor": "supplier_name",
}

_FILA = {
    "fecha": "2024-03-05",
    "articulo": "Vela aromatica 200g",
    "cantidad": "10",
    "total": "1000",
    "envio": "300",
    "comprobante": "A-0001",
    "proveedor": "Distribuidora Sur",
}

_DECISION = {
    "context_id": _CTX,
    "base": "monto_incluye",
    "shared_shipping": "por_subtotal",
    "line_shipping": "gasto_aparte",
}


def _column_mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_column": src,
            "target_field": target,
            "context_id": _CTX,
            "entity_type": "expense",
        }
        for src, target in _MAPEO.items()
    ]


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key="uploads/test/uuid/compras.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="gastos",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "mapping_contexts": [
                {
                    "context_id": _CTX,
                    "label": "Compras",
                    "entity_type": "expense",
                    "source_kind": "sheet",
                    "headers": list(_MAPEO),
                    "fields": None,
                    "preview_rows": [],
                    "row_count": 1,
                }
            ],
            "gastos_detectados": [{**_FILA, "__context__": _CTX}],
            "ventas_detectadas": [],
            "stock_detectado": [],
        },
    )
    db_session.add(record)
    await db_session.commit()
    return record


@pytest.fixture
def habilitar(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _habilitar(tenant: Tenant) -> None:
        monkeypatch.setattr(
            get_settings(),
            "PURCHASE_COST_ROLLOUT_TENANT_IDS",
            [str(tenant.tenant_id)],
        )

    return _habilitar


class TestElPreviewDelRepartoEstaGateado:
    async def test_tenant_fuera_de_la_allowlist_403(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """La allowlist vacía es el default: nadie habilitado."""
        assert get_settings().PURCHASE_COST_ROLLOUT_TENANT_IDS == []
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/purchase-groups",
            json={"column_mappings": _column_mappings()},
            headers=auth_headers,
        )
        assert response.status_code == 403
        # El mensaje habla del negocio, no de la operación: nunca la variable de
        # entorno ni la palabra "allowlist".
        detail = response.json()["detail"]
        assert "costos de compra" in detail
        assert "PURCHASE_COST_ROLLOUT_TENANT_IDS" not in detail

    async def test_tenant_habilitado_pasa(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        sample_tenant: Tenant,
        habilitar: Any,
    ) -> None:
        """Control: sin esto, un 403 constante daría el test de arriba por bueno."""
        habilitar(sample_tenant)
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/purchase-groups",
            json={"column_mappings": _column_mappings()},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["sheets"]


class TestElConfirmRechazaLaDecisionDeCosto:
    async def test_tenant_fuera_de_la_allowlist_422_con_traza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": _column_mappings(),
                "confirmed_fields": {"gastos": True},
                "context_confirmed": {_CTX: True},
                "purchase_cost_decisions": [_DECISION],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert "costos de compra" in response.json()["detail"]

        # El archivo NO quedó tomado: el rechazo es pre-lease.
        await db_session.refresh(archivo)
        assert archivo.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION

        eventos = list(
            (
                await db_session.execute(
                    select(PipelineEvent).where(PipelineEvent.stage == "reject")
                )
            )
            .scalars()
            .all()
        )
        assert len(eventos) == 1
        assert eventos[0].detail is not None
        assert eventos[0].detail["motivo"] == "motor_de_costos_no_habilitado"
        assert eventos[0].detail["contextos"] == [_CTX]

    async def test_sin_decision_de_costo_el_import_pasa_igual(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """La compuerta gobierna SÓLO el motor de costos.

        Un tenant deshabilitado tiene que poder seguir importando su libro de
        compras como siempre — si el rechazo alcanzara a cualquier confirm, la
        compuerta habría apagado el producto en vez de una función.
        """
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": _column_mappings(),
                "confirmed_fields": {"gastos": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    async def test_tenant_habilitado_acepta_la_decision(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        sample_tenant: Tenant,
        habilitar: Any,
    ) -> None:
        """Control del 422: adentro de la allowlist la decisión se honra."""
        habilitar(sample_tenant)
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": _column_mappings(),
                "confirmed_fields": {"gastos": True},
                "context_confirmed": {_CTX: True},
                "purchase_cost_decisions": [_DECISION],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
