"""F-H4 desde la pantalla: una planilla sin columna de monto se puede confirmar.

Es la compuerta que evita repetir el agujero de F-H3.e. El cálculo de
`precio × cantidad` puede estar cableado y probado en el importador, pero si el
confirm sigue exigiendo `amount` mapeado, el archivo que motivó toda la fase
—precio unitario y cantidad, sin total— rebota con 422 y la derivación no se
alcanza nunca desde la UI.

Por eso el test entra por HTTP y verifica la venta persistida, no la función.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

_CTX = "sheet:ventas marzo"


def _summary() -> dict[str, Any]:
    """Una planilla de ventas REAL: precio unitario y cantidad, sin total."""
    filas = [
        {
            "Fecha": "2024-03-10",
            "Artículo": "Vela aromática 200g",
            "P. unitario": "150.50",
            "Cant.": "3",
            "__context__": _CTX,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "multi_sheet": True,
        "has_venta": True,
        "row_count": 1,
        "ventas_detectadas": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "ventas marzo",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["Fecha", "Artículo", "P. unitario", "Cant."],
                "fields": None,
                "preview_rows": filas,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas_marzo.xlsx",
        s3_key="uploads/test/uuid/ventas_marzo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "sale",
    }


async def _confirmar(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    mapeos: list[dict[str, Any]],
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": mapeos,
            "confirmed_fields": {"ventas": True},
            "context_confirmed": {_CTX: True},
        },
        headers=auth_headers,
    )


async def _motivo_del_rechazo(db_session: AsyncSession) -> dict[str, Any]:
    eventos = (
        (await db_session.execute(select(PipelineEvent).where(PipelineEvent.stage == "reject")))
        .scalars()
        .all()
    )
    assert len(eventos) == 1, f"se esperaba 1 rechazo, hay {len(eventos)}"
    detail = eventos[0].detail
    assert detail is not None
    return detail


class TestElMontoDejaDeSerObligatorio:
    async def test_precio_y_cantidad_alcanzan_para_confirmar(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            [
                _map("Fecha", "transaction_date"),
                _map("Artículo", "product_name"),
                _map("P. unitario", "unit_price"),
                _map("Cant.", "quantity"),
            ],
        )
        assert response.status_code == 200, response.text

        venta = (
            (await db_session.execute(select(SaleEntry))).scalars().all()
        )
        assert len(venta) == 1
        assert venta[0].amount == Decimal("451.50")  # 150.50 × 3
        assert venta[0].quantity == 3
        assert venta[0].unit_price == Decimal("150.50")

    async def test_el_aviso_dice_que_el_monto_se_calculo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """El monto no lo trajo el archivo: el usuario tiene que enterarse por la
        respuesta, no revisando fila por fila."""
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            [
                _map("Fecha", "transaction_date"),
                _map("Artículo", "product_name"),
                _map("P. unitario", "unit_price"),
                _map("Cant.", "quantity"),
            ],
        )
        assert response.status_code == 200, response.text
        avisos = " ".join(response.json()["warnings"])
        assert "no traían el monto" in avisos
        assert "precio unitario × cantidad" in avisos


class TestLaAlternativaIncompletaNoAlcanza:
    """Media alternativa no calcula nada, así que el monto sigue obligatorio."""

    async def test_solo_precio_unitario_sigue_pidiendo_el_monto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            [
                _map("Fecha", "transaction_date"),
                _map("P. unitario", "unit_price"),
            ],
        )
        assert response.status_code == 422
        assert "Monto de venta" in response.json()["detail"]

        detail = await _motivo_del_rechazo(db_session)
        assert detail["motivo"] == "requeridos_sin_mapear"
        assert detail["faltantes"] == ["amount"]

    async def test_solo_cantidad_sigue_pidiendo_el_monto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            [_map("Fecha", "transaction_date"), _map("Cant.", "quantity")],
        )
        assert response.status_code == 422
        assert "Monto de venta" in response.json()["detail"]


class TestUnCampoPropioNoCubre:
    async def test_un_campo_propio_llamado_amount_no_cubre_el_monto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """El caso ASTERIA aplicado al monto: el campo propio guarda el dato pero
        no lo vuelve el monto del importador, así que el requerido sigue abierto."""
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            [
                _map("Fecha", "transaction_date"),
                _map("P. unitario", "custom_field:amount"),
            ],
        )
        assert response.status_code == 422
        assert "Monto de venta" in response.json()["detail"]

    async def test_la_alternativa_tampoco_se_cubre_con_campos_propios(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """Un `custom_field:unit_price` no es un precio unitario para el
        importador: no lo lee, así que no habilita a calcular nada."""
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            [
                _map("Fecha", "transaction_date"),
                _map("P. unitario", "custom_field:unit_price"),
                _map("Cant.", "custom_field:quantity"),
            ],
        )
        assert response.status_code == 422
        assert "Monto de venta" in response.json()["detail"]
