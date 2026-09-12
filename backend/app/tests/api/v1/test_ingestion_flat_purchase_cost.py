"""Un archivo de UNA sola tabla también cobra el envío. Y qué queda rechazado.

Historia, porque el contrato de este archivo cambió y el motivo importa
---------------------------------------------------------------------
Hasta E6a, **cualquier** archivo plano con una columna de envío mapeada se
rechazaba con 422. El rechazo estaba bien puesto: el camino plano no cobraba el
flete y aceptarlo dejaba la compra con un costo más bajo que el real y un margen
inflado que nadie iba a salir a buscar. Pero la causa nunca fue el formato — eran
tres defectos del importador: el cobro vivía en un closure del camino multi-hoja
(inalcanzable desde el plano), la clave de contexto se descartaba (así que la
decisión del usuario se validaba, se aceptaba y se ignoraba) y los avisos de costo
no llegaban a ``counts``.

Arreglados los tres y probada la **paridad de efectos entre formatos**
(``test_paridad_tabla_vs_hoja``), el rechazo total ya no describe ninguna
limitación: **una sola tabla también agrupa por comprobante**, porque lo que
determina la agrupación son los identificadores de la fila —proveedor y número—,
no que exista una hoja aparte.

Lo que queda rechazado es una cosa sola y específica: **decisiones de costo sobre
mapeos que no dicen a qué hoja pertenecen**. Ahí el importador buscaría la
decisión bajo una clave que nadie escribió y se comportaría como si no existiera,
que es exactamente el silencio que el 422 original vino a impedir.
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
from app.persistence.models.transaction import ExpenseEntry

_CTX = "sheet:Compras"

#: Dos líneas del MISMO comprobante con el flete repetido — la forma en que lo
#: escribe una planilla real. Cobrarlo por fila lo duplicaría.
_FILAS = [
    {
        "fecha": "2024-03-05",
        "nro": "0001-00000123",
        "proveedor": "Distribuidora Sur",
        "articulo": "Vela aromatica 200g",
        "cantidad": "10",
        "total": "1000",
        "envio": "300",
    },
    {
        "fecha": "2024-03-05",
        "nro": "0001-00000123",
        "proveedor": "Distribuidora Sur",
        "articulo": "Difusor bambu",
        "cantidad": "5",
        "total": "800",
        "envio": "300",
    },
]

_MAPEO = {
    "fecha": "expense_date",
    "nro": "invoice_number",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
}


def _summary_plano(*, con_contexto: bool) -> dict[str, Any]:
    """Sin ``multi_sheet``: el importador toma el camino de una sola tabla.

    ``con_contexto`` decide si el archivo declara su hoja. Es la diferencia entre
    los dos casos que este archivo separa: con hoja, la decisión del usuario se
    puede ubicar; sin hoja, no.
    """
    base: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "row_count": len(_FILAS),
        "gastos_detectados": [dict(f) for f in _FILAS],
    }
    if con_contexto:
        base["mapping_contexts"] = [
            {
                "context_id": _CTX,
                "label": "Compras",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": list(_MAPEO),
                "fields": None,
                "preview_rows": [],
                "row_count": len(_FILAS),
            }
        ]
        base["gastos_detectados"] = [{**f, "__context__": _CTX} for f in _FILAS]
    return base


async def _crear(db: AsyncSession, tenant: Tenant, summary: dict[str, Any]) -> UploadedFile:
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


def _mappings(*, context_id: str | None) -> list[dict[str, Any]]:
    return [
        {
            "source_column": src,
            "target_field": target,
            **({"context_id": context_id, "entity_type": "expense"} if context_id else {}),
        }
        for src, target in _MAPEO.items()
    ]


@pytest_asyncio.fixture
async def plano_con_hoja(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _crear(db_session, sample_tenant, _summary_plano(con_contexto=True))


@pytest_asyncio.fixture
async def plano_sin_hoja(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _crear(db_session, sample_tenant, _summary_plano(con_contexto=False))


async def _logistica(db: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


class TestElPlanoCobraElEnvio:
    async def test_una_sola_tabla_con_envio_se_importa_y_cobra_el_flete(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano_con_hoja: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Lo que antes era un 422. Ahora entra, y el flete se cobra UNA vez."""
        response = await client.post(
            f"/api/v1/ingestion/files/{plano_con_hoja.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=_CTX),
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

        fletes = await _logistica(db_session, sample_tenant)
        assert len(fletes) == 1, (
            f"{len(fletes)} fletes para un comprobante de dos líneas: el envío "
            "repetido en cada línea se cobró más de una vez"
        )
        assert str(fletes[0].amount) == "300.00"
        # Y el flete es OPEX de logística, igual que por el camino multi-hoja: el
        # mismo hecho de negocio no puede quedar clasificado de dos formas según
        # por dónde entró el archivo.
        assert fletes[0].expense_type == "OPEX"

    async def test_sin_columna_de_envio_no_cambia_nada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano_con_hoja: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El archivo que no mapea flete no paga ningún efecto nuevo."""
        mapeo = [m for m in _mappings(context_id=_CTX) if m["source_column"] != "envio"]
        response = await client.post(
            f"/api/v1/ingestion/files/{plano_con_hoja.id}/confirm",
            json={"column_mappings": mapeo, "confirmed_fields": {"gastos": True}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert not await _logistica(db_session, sample_tenant)


class TestLoQueSigueRechazado:
    """Decisiones de costo sobre mapeos que no dicen a qué hoja pertenecen.

    Es el único caso que queda: sin hoja, el importador buscaría la decisión bajo
    una clave que nadie escribió y se comportaría como si no existiera. Aceptarlo
    sería volver al silencio original con otra cara.
    """

    async def test_decision_de_envio_sin_hoja_en_el_mapeo_422_antes_del_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano_sin_hoja: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{plano_sin_hoja.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
                "shipping_decisions": [
                    {"context_id": _CTX, "action": "una_por_hoja"}
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        # Nombra el archivo y dice qué hacer, sin nombres técnicos de campos.
        assert "compras_marzo.xlsx" in detail
        assert "a qué hoja" in detail
        assert "shipping_cost" not in detail

        # Pre-lease: el archivo sigue disponible para volver a confirmar.
        await db_session.refresh(plano_sin_hoja)
        assert plano_sin_hoja.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION

    async def test_deja_traza_en_pipeline_events(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano_sin_hoja: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Sin traza, un 422 pre-lease no deja NI UNA fila y diagnosticarlo
        después exige reconstruir el caso a mano (los tres 422 de ASTERIA)."""
        await client.post(
            f"/api/v1/ingestion/files/{plano_sin_hoja.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
                "purchase_cost_decisions": [
                    {"context_id": _CTX, "base": "monto_sin_ajustes"}
                ],
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
        assert detalle["motivo"] == "costos_de_compra_sin_hoja"
        assert detalle["decisiones_costo"] is True
        assert detalle["http_status"] == 422

    async def test_sin_decisiones_un_mapeo_sin_hoja_no_se_rechaza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        plano_sin_hoja: UploadedFile,
    ) -> None:
        """El rechazo es por la DECISIÓN que no se puede ubicar, no por el mapeo.

        Un archivo sin hoja y sin decisiones no tiene nada que perderse: la
        agrupación del envío sale de los identificadores de la fila. Rechazarlo
        sería volver al rechazo ciego que esta fase sacó.
        """
        response = await client.post(
            f"/api/v1/ingestion/files/{plano_sin_hoja.id}/confirm",
            json={
                "column_mappings": _mappings(context_id=None),
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
