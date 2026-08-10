"""Un archivo de UNA sola tabla con costos de compra se rechaza, no se importa a medias.

El camino plano del importador no cobra el envío ni aplica las decisiones de
costo, y no lo hace de tres maneras a la vez: `_cobrar_envios_de_la_hoja` es un
closure anidado dentro del camino multi-hoja (inalcanzable desde el plano), el
plano llama al planificador con `ctx_id=None` —que busca la decisión bajo la
clave `""` mientras la API la manda con el `context_id` real— y los avisos de
costo nunca llegan a `counts`.

El resultado es la peor combinación posible: la decisión se valida, el usuario la
ve aceptada, el import la ignora y no queda rastro. La compra entra con un costo
más bajo que el real y con él un margen inflado que nadie va a salir a buscar.

Arreglar el camino plano es otra fase. Lo que no se puede hacer mientras tanto es
aceptar el archivo en silencio.

El rechazo es GLOBAL, no gateado por tenant: no cobrar un envío que el usuario
mapeó es incorrecto con el motor de costos prendido o apagado.
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

_CTX = "sheet:Compras"

_FILA = {
    "fecha": "2024-03-05",
    "articulo": "Vela aromatica 200g",
    "cantidad": "10",
    "total": "1000",
    "envio": "300",
}

_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
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


class TestElPlanoConCostosSeRechaza:
    async def test_envio_mapeado_en_archivo_plano_422_antes_del_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{plano.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        # Nombra el archivo y dice las dos salidas posibles, sin nombres técnicos.
        assert "compras_marzo.xlsx" in detail
        assert "hojas separadas" in detail
        assert "shipping_cost" not in detail

        # Pre-lease: el archivo sigue disponible para volver a confirmar.
        await db_session.refresh(plano)
        assert plano.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION

    async def test_deja_traza_en_pipeline_events(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Sin traza, un 422 pre-lease no deja NI UNA fila y diagnosticarlo
        después exige reconstruir el caso a mano (los tres 422 de ASTERIA)."""
        await client.post(
            f"/api/v1/ingestion/files/{plano.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )
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
        detalle = eventos[0].detail
        assert detalle is not None
        assert detalle["motivo"] == "costos_de_compra_en_archivo_plano"
        assert detalle["columnas"] == ["shipping_cost"]
        assert detalle["http_status"] == 422

    async def test_una_decision_de_costo_sola_tambien_rechaza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
    ) -> None:
        """Sin columna de envío pero con decisión: el plano igual la descarta.

        Es el caso más engañoso de los tres —la decisión se valida, se acepta y se
        ignora— así que no puede pasar sólo porque no haya columna de flete.
        """
        mapeo = {k: v for k, v in _MAPEO.items() if k != "envio"}
        response = await client.post(
            f"/api/v1/ingestion/files/{plano.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None, mapeo=mapeo),
                "confirmed_fields": {"gastos": True},
                "purchase_cost_decisions": [
                    {
                        "context_id": _CTX,
                        "base": "monto_incluye",
                        "shared_shipping": "por_subtotal",
                        "line_shipping": "gasto_aparte",
                    }
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert "costo de compra" in response.json()["detail"]

    async def test_el_mismo_archivo_sin_costos_importa_igual(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano: UploadedFile,
    ) -> None:
        """Control 1: el rechazo alcanza a los costos, no al archivo plano.

        Sin esto, romper todos los imports planos pasaría el test de arriba.
        """
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

    async def test_el_mismo_contenido_como_libro_multihoja_importa(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Control 2, el que importa: MISMAS filas, MISMO mapeo con envío, pero en
        un libro con su hoja declarada → 200.

        Es la salida que el mensaje del 422 le ofrece al usuario. Si esto fuera
        rojo, el rechazo habría apagado el camino bueno y el consejo sería falso.
        """
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
