"""F-C.c4 — el 422 de requeridos sin mapear dice POR QUÉ, no sólo qué falta.

El mensaje ya nombraba el campo con su etiqueta en castellano («Monto de venta»
y no `amount`), que fue el primer arreglo. Pero seguía siendo una lista: la
persona leía qué campo falta y no qué se pierde si no lo mapea — y son destinos
distintos según el campo. Una venta sin monto queda en «Otros» y se puede
completar desde ahí; un gasto sin monto se descarta y no deja rastro. Decidir si
vale la pena arreglar la planilla exige saber cuál de las dos cosas va a pasar.

El texto sale de `REQUIRED_REASONS`, el mismo que sirve el catálogo: el banner de
la pantalla y el rechazo del backend no pueden explicar cosas distintas sobre el
mismo campo.

Cubre los DOS caminos —plano (legacy, sin `mapping_contexts`) y multi-hoja—
porque son dos bloques de código separados y el arreglo de uno no toca al otro.
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import required_reason
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.tenant import Tenant

_CTX = "sheet:ventas marzo"


def _archivo(tenant_id: Any, summary: dict[str, Any]) -> UploadedFile:
    return UploadedFile(
        tenant_id=tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key="uploads/test/uuid/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )


@pytest_asyncio.fixture
async def archivo_plano(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    """Sin `mapping_contexts`: el camino legacy de un CSV suelto."""
    filas = [{"Fecha": "2026-03-01", "Cliente": "Kiosco El Sol"}]
    record = _archivo(
        sample_tenant.tenant_id,
        {
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": filas,
        },
    )
    db_session.add(record)
    await db_session.commit()
    return record


@pytest_asyncio.fixture
async def archivo_multihoja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> UploadedFile:
    filas = [{"Fecha": "2026-03-01", "Cliente": "Kiosco El Sol", "__context__": _CTX}]
    record = _archivo(
        sample_tenant.tenant_id,
        {
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
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
                    "headers": ["Fecha", "Cliente"],
                    "fields": None,
                    "preview_rows": filas,
                    "row_count": 1,
                }
            ],
        },
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _detalle_del_rechazo(db_session: AsyncSession) -> dict[str, Any]:
    result = await db_session.execute(
        select(PipelineEvent).where(PipelineEvent.stage == "reject")
    )
    eventos = list(result.scalars().all())
    assert len(eventos) == 1, f"se esperaba 1 evento 'reject', hay {len(eventos)}"
    detail = eventos[0].detail
    assert detail is not None
    return detail


class TestElRechazoExplicaPorQue:
    async def test_camino_plano(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo_plano: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo_plano.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                ],
                "confirmed_fields": {"ventas": True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]

        assert "Monto de venta" in detail
        assert required_reason("sale", "amount") in detail
        # Nunca el nombre técnico: leer «Campos requeridos sin mapear: amount» es
        # exactamente la queja que originó la fase.
        assert "amount" not in detail
        assert "transaction_date" not in detail

    async def test_camino_multihoja(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo_multihoja: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo_multihoja.id}/confirm",
            json={
                "column_mappings": [
                    {
                        "source_column": "Fecha",
                        "target_field": "transaction_date",
                        "context_id": _CTX,
                        "entity_type": "sale",
                    },
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]

        # La hoja se nombra con su etiqueta legible, no con el context_id crudo.
        assert "ventas marzo" in detail
        assert "Monto de venta" in detail
        assert required_reason("sale", "amount") in detail
        assert "amount" not in detail
        assert "transaction_date" not in detail

    async def test_la_traza_guarda_el_mismo_motivo_que_leyo_la_persona(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo_multihoja: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Con sólo los nombres técnicos en `pipeline_events`, reconstruir qué
        decía la pantalla exigía saber de memoria qué texto servía ese deploy."""
        await client.post(
            f"/api/v1/ingestion/files/{archivo_multihoja.id}/confirm",
            json={
                "column_mappings": [
                    {
                        "source_column": "Fecha",
                        "target_field": "transaction_date",
                        "context_id": _CTX,
                        "entity_type": "sale",
                    },
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )

        detail = await _detalle_del_rechazo(db_session)
        assert detail["motivo"] == "requeridos_sin_mapear"
        assert detail["faltantes"] == ["amount"]
        assert detail["motivos"] == {"amount": required_reason("sale", "amount")}

    async def test_un_campo_sin_motivo_no_rompe_el_mensaje(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo_multihoja: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Control: el mensaje se arma con la etiqueta aunque no haya motivo
        escrito, y la traza no inventa uno.

        `customer` sólo requiere el nombre y ese sí tiene motivo, así que el caso
        se fuerza reasignando la hoja a Clientes: lo que se verifica es que la
        ausencia de motivo degrade a la etiqueta sola, sin `None` en el texto.
        """
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo_multihoja.id}/confirm",
            json={
                "column_mappings": [
                    {
                        "source_column": "Fecha",
                        "target_field": "custom_field:fecha_suelta",
                        "context_id": _CTX,
                        "entity_type": "customer",
                    },
                ],
                "confirmed_fields": {"clientes": True},
                "context_entity": {_CTX: "customer"},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "None" not in detail
        assert required_reason("customer", "name") in detail
