"""Un confirm rechazado deja rastro, y uno exitoso deja con qué mapeo importó.

Los tres 422 de ASTERIA (2026-07-31) no escribieron una sola fila en
`pipeline_events`: `_emit_confirm_failure` se define DESPUÉS de
`acquire_import_lease`, y las validaciones rebotan antes del lease a propósito
(una request que va a rebotar nunca lo toma). Diagnosticarlos exigió reconstruir
el caso a mano desde capturas de pantalla.

Y el `STAGE_CONFIRM` guardaba counts y confirmed_fields pero NO el mapeo usado,
así que saber con qué columna se cargó un precio obligaba a inferirlo de los
alias aprendidos del tenant — que pudieron cambiar después o haberse aprendido en
otro archivo.
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.tenant import Tenant

_CTX = "sheet:precios y stock"


def _summary() -> dict[str, Any]:
    filas = [
        {
            "Productos": "Vela aromática 200g",
            "Precio de compra": "1200",
            "Precio de venta final": "2100",
            "__context__": _CTX,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "precios y stock",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["Productos", "Precio de compra", "Precio de venta final"],
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
        original_filename="ASTERIA_home_deco.xlsx",
        s3_key="uploads/test/uuid/asteria.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="stock",
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
        "entity_type": "product",
    }


async def _eventos(db_session: AsyncSession, stage: str) -> list[PipelineEvent]:
    result = await db_session.execute(
        select(PipelineEvent).where(PipelineEvent.stage == stage)
    )
    return list(result.scalars().all())


async def _detalle_unico(db_session: AsyncSession, stage: str) -> dict[str, Any]:
    """El detail del único evento de esa etapa. Falla si no hay exactamente uno:
    la ausencia de traza es justamente lo que estos tests vigilan."""
    eventos = await _eventos(db_session, stage)
    assert len(eventos) == 1, f"se esperaba 1 evento '{stage}', hay {len(eventos)}"
    detail = eventos[0].detail
    assert detail is not None
    return detail


class TestTrazaDelConfirm:
    async def test_requerido_en_campo_personalizado_deja_traza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """El caso exacto de ASTERIA: `name` movido a un campo personalizado."""
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "custom_field:nombre_del_producto"),
                    _map("Precio de venta final", "sale_price_ars"),
                ],
                "confirmed_fields": {"productos": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        # El mensaje nombra la hoja con su label legible, no el context_id crudo.
        assert "precios y stock" in response.json()["detail"]

        detail = await _detalle_unico(db_session, "reject")
        assert detail["motivo"] == "requeridos_sin_mapear"
        assert detail["http_status"] == 422
        assert detail["faltantes"] == ["name"]
        assert detail["context_id"] == _CTX

    async def test_colision_escalar_deja_traza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "name"),
                    _map("Precio de compra", "sale_price_ars"),
                    _map("Precio de venta final", "sale_price_ars"),
                ],
                "confirmed_fields": {"productos": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

        detail = await _detalle_unico(db_session, "reject")
        assert detail["motivo"] == "colision_campo_escalar"

    async def test_la_traza_no_lleva_valores_de_fila(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Nombres de columna y campos sí; datos del negocio nunca."""
        await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "custom_field:nombre_del_producto"),
                    _map("Precio de venta final", "sale_price_ars"),
                ],
                "confirmed_fields": {"productos": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        serializado = str(await _detalle_unico(db_session, "reject"))
        assert "Vela aromática" not in serializado
        assert "1200" not in serializado
        assert "2100" not in serializado

    async def test_confirm_exitoso_guarda_con_que_mapeo_importo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Evidencia de nivel 1: qué columna alimentó cada campo, en este archivo.

        Sin esto, saber si un producto quedó con el costo cargado como precio de
        venta obliga a inferirlo de los alias aprendidos del tenant, que no
        demuestran nada sobre un import concreto.
        """
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "name"),
                    _map("Precio de compra", "unit_cost_ars"),
                    _map("Precio de venta final", "sale_price_ars"),
                ],
                "confirmed_fields": {"productos": True},
                "context_confirmed": {_CTX: True},
                "stock_treatment": {_CTX: "opening_balance"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

        detail = await _detalle_unico(db_session, "confirm")
        mappings = detail["mappings"]["context"][_CTX]
        assert mappings["Precio de compra"] == "unit_cost_ars"
        assert mappings["Precio de venta final"] == "sale_price_ars"
        assert mappings["Productos"] == "name"
        # También queda registrado cómo se trató el stock de cada hoja.
        assert detail["stock_treatment"] == {_CTX: "opening_balance"}
        # Sin valores de fila.
        assert "Vela aromática" not in str(detail["mappings"])


class TestDesgloseDeTiempos:
    """F-T — el confirm dice dónde se le fue el tiempo, no sólo cuánto tardó.

    `latency_ms` medía únicamente `insert_confirmed_data`: un confirm que tarda
    treinta segundos validando y uno que tarda uno se reportaban igual.
    """

    async def test_un_confirm_exitoso_publica_el_desglose_por_etapa(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger: Any,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "name"),
                    _map("Precio de venta final", "sale_price_ars"),
                ],
                "confirmed_fields": {"productos": True},
                "context_confirmed": {_CTX: True},
                "stock_treatment": {_CTX: "opening_balance"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

        detail = await _detalle_unico(db_session, "confirm")
        etapas = detail["timings_ms"]["stages"]
        # Las etapas del camino feliz, de punta a punta. Si alguna desaparece, el
        # desglose deja de cubrir el confirm y vuelve a haber tiempo sin atribuir.
        assert {
            "validaciones_pre_lease",
            "lease",
            "snapshot_maestros",
            "import",
            "ledger_reversa",
            "aprendizaje_mapeos",
            "finalize_lease",
        } <= set(etapas)
        # El import declara CUÁNTAS filas movió: un tiempo sin denominador no se
        # puede comparar entre dos archivos.
        assert etapas["import"]["rows"] == 1
        # El total cubre todo el request, no sólo el import.
        assert detail["timings_ms"]["total_ms"] >= etapas["import"]["ms"]

    async def test_un_confirm_rechazado_tambien_dice_cuanto_tardo_en_rebotar(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Un 422 también hace esperar. Sin esto, «rebotó» y «rebotó a los veinte
        segundos» se leen igual en la traza."""
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "custom_field:nombre_del_producto"),
                    _map("Precio de venta final", "sale_price_ars"),
                ],
                "confirmed_fields": {"productos": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

        detail = await _detalle_unico(db_session, "reject")
        assert detail["timings_ms"]["total_ms"] >= 0
        # El rechazo es PREVIO al lease, así que no hay etapas cerradas todavía:
        # lo que importa es el total. Afirmar que hay etapas sería afirmar que el
        # confirm llegó más lejos de lo que llegó.
        assert detail["timings_ms"]["stages"] == {}
