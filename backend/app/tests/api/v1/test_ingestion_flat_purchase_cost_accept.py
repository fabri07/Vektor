"""F-H6.f (8d) — un archivo de UNA sola tabla con costos de compra se acepta y
cobra el envío, a través del endpoint HTTP real.

Reemplaza a `test_ingestion_flat_purchase_cost_reject.py`: hasta 8b, el camino
plano no cobraba el envío ni aplicaba las decisiones de costo (tres razones
distintas, ver `ingestion_import_service.py::_cobrar_envios_de_la_hoja` y
`_planificar_costos_de_la_hoja`), así que el confirm rechazaba el archivo con
422 en vez de importarlo mal. Con 8a-8b arreglado (refactor + wireo del camino
plano + V24/V25), el guard 422 quedó como una red de seguridad sobre un bug ya
cerrado — se retira acá, y la compuerta pasa a ser "el archivo entra y cobra
bien", no "el archivo se rechaza".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


@pytest.fixture(autouse=True)
def tenant_habilitado(monkeypatch: pytest.MonkeyPatch, sample_tenant: Tenant) -> None:
    """El motor de costos de compra sale con la allowlist VACÍA (nadie
    habilitado) — mismo criterio que `test_ingestion_purchase_groups.py`."""
    monkeypatch.setattr(
        get_settings(),
        "PURCHASE_COST_ROLLOUT_TENANT_IDS",
        [str(sample_tenant.tenant_id)],
    )

_CTX = "sheet:Compras"

_FILA = {
    "fecha": "2024-03-05",
    "articulo": "Vela aromatica 200g",
    "cantidad": "10",
    "total": "1000",
    "envio": "300",
    "comprobante": "A-0001",
    "proveedor": "Distribuidora Sur",
}

_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
    "comprobante": "invoice_number",
    "proveedor": "supplier_name",
}


def _summary_plano() -> dict[str, Any]:
    """Sin `mapping_contexts` ni `multi_sheet`: el importador toma el camino de
    una sola tabla (`inferred_type != "mixed" and not multi_sheet`)."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "row_count": 1,
        "gastos_detectados": [dict(_FILA)],
    }


def _summary_multihoja() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_gasto": True,
        "row_count": 1,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Compras",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": list(_FILA),
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            }
        ],
        "gastos_detectados": [{**_FILA, "__context__": _CTX}],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


async def _crear(
    db: AsyncSession, tenant: Tenant, summary: dict[str, Any]
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras_marzo.xlsx",
        s3_key="uploads/test/uuid/compras_marzo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="gastos",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    return record


def _mappings(
    *, context_id: str | None, mapeo: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "source_column": src,
            "target_field": target,
            **({"context_id": context_id, "entity_type": "expense"} if context_id else {}),
        }
        for src, target in (mapeo or _MAPEO).items()
    ]


@pytest_asyncio.fixture
async def plano(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _crear(db_session, sample_tenant, _summary_plano())


async def _logistica(db: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                )
            )
        )
        .scalars()
        .all()
    )


class TestElPlanoConCostosSeAcepta:
    async def test_envio_mapeado_en_archivo_plano_200_y_cobra_el_envio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{plano.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

        envios = await _logistica(db_session, sample_tenant)
        assert len(envios) == 1
        assert envios[0].amount == Decimal("300.00")

    async def test_una_decision_de_costo_se_aplica(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """La decisión de costo del `context_id` sintético ("table") se lee y
        se aplica — antes (V24) se validaba, se aceptaba y se ignoraba. Deja
        mapeada `shipping_cost`: `por_subtotal` exige esa columna (regla #3
        de `validate_purchase_cost_decisions`), y sacarla haría que el 200
        dependa de una validación laxa en vez de la decisión real."""
        response = await client.post(
            f"/api/v1/ingestion/files/{plano.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
                "purchase_cost_decisions": [
                    {
                        "context_id": "table",
                        "base": "monto_incluye",
                        "shared_shipping": "por_subtotal",
                        "line_shipping": "gasto_aparte",
                    }
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    async def test_el_mismo_archivo_sin_costos_sigue_importando(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
    ) -> None:
        """Control: un archivo plano SIN columnas de envío sigue funcionando
        igual que siempre (8a fue un refactor puro, sin cambio de comportamiento
        para el resto de las compras)."""
        mapeo = {k: v for k, v in _MAPEO.items() if k != "envio"}
        response = await client.post(
            f"/api/v1/ingestion/files/{plano.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None, mapeo=mapeo),
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    async def test_el_mismo_contenido_como_libro_multihoja_da_el_mismo_envio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Equivalencia end-to-end: MISMAS filas, MISMO mapeo con envío, como
        libro multi-hoja → mismo cobro que el camino plano de arriba."""
        archivo = await _crear(db_session, sample_tenant, _summary_multihoja())
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=_CTX),
                "confirmed_fields": {"gastos": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

        envios = await _logistica(db_session, sample_tenant)
        assert len(envios) == 1
        assert envios[0].amount == Decimal("300.00")
