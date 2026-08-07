"""F-H3.d.6 — un replay que no se puede validar no se confirma.

En un archivo de UNA sola tabla donde las mismas filas dan la venta y dan de alta
el producto, el gate de `historical_replay` no tiene saldo contra el cual evaluar:
lo carga el propio archivo, en la misma pasada. Antes el importador se abstenía —
lo dejaba anotado en `counts["replay_sin_gatear"]` y seguía—, así que las ventas
sin respaldo entraban a los libros igual y el import se reportaba como un replay.
Justo lo contrario de lo que el modo promete.

Se rechaza con 422 en vez de degradar a `informational` en silencio, por la misma
regla que ya rige en `resolve_inventory_effects`: un override que no se puede
honrar no se ignora, porque significa que el usuario cree haber decidido algo
sobre su inventario que no va a pasar.

El rechazo va ANTES del lease, donde no hay nada a medio importar ni lease que
compensar — y deja `STAGE_REJECT` en `pipeline_events`, que es lo que hace
diagnosticable un 422 sin reconstruir el caso a mano.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_CTX = "table:0"
_LABEL = "Movimientos 2024"
_PRODUCTO = "Vela aromatica 200g"


def _summary() -> dict[str, Any]:
    """Una sola tabla que es a la vez catálogo y libro de ventas.

    `has_producto` y `has_venta` juntos es lo que hace que el confirm cree el
    producto Y registre la venta desde LA MISMA fila — el caso sin saldo previo.
    """
    # Dos filas del mismo producto: la del 03/03 entra en un stock de 2, la del
    # 10/03 no. Con una sola fila no se podría distinguir un gate que elige de uno
    # que apaga el archivo entero.
    filas = [
        {
            "fecha": "2024-03-03",
            "producto": _PRODUCTO,
            "cantidad": "2",
            "monto": "4200",
            "__context__": _CTX,
        },
        {
            "fecha": "2024-03-10",
            "producto": _PRODUCTO,
            "cantidad": "6",
            "monto": "12600",
            "__context__": _CTX,
        },
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "general",
        "multi_sheet": False,
        "has_venta": True,
        "has_producto": True,
        "row_count": 2,
        "headers": ["fecha", "producto", "cantidad", "monto"],
        "ventas_detectadas": filas,
        "stock_detectado": filas,
        "preview_rows": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": _LABEL,
                "source_kind": "table",
                "entity_type": "sale",
                "headers": ["fecha", "producto", "cantidad", "monto"],
                "fields": None,
                "preview_rows": filas,
                "row_count": 2,
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="movimientos.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/movimientos.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="ingestion",
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


_MAPEOS = [
    _map("fecha", "transaction_date"),
    _map("producto", "product_name"),
    _map("cantidad", "quantity"),
    _map("monto", "amount"),
]


def _payload(efecto: str, *, productos: bool = True) -> dict[str, Any]:
    confirmados: dict[str, bool] = {"ventas": True}
    if productos:
        confirmados["productos"] = True
    return {
        "column_mappings": _MAPEOS,
        "confirmed_fields": confirmados,
        "context_confirmed": {_CTX: True},
        "inventory_effect": {_CTX: efecto},
    }


async def _confirmar(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    payload: dict[str, Any],
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json=payload,
        headers=auth_headers,
    )


async def _cuantas(db_session: AsyncSession, tenant: Tenant, modelo: Any) -> int:
    result = await db_session.execute(
        select(modelo).where(modelo.tenant_id == tenant.tenant_id)
    )
    return len(list(result.scalars().all()))


@pytest.mark.asyncio
class TestReplayNoGateable:
    async def test_rechaza_el_replay_que_no_puede_validar(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        response = await _confirmar(
            client, auth_headers, archivo, _payload("historical_replay")
        )

        assert response.status_code == 422
        detalle = response.json()["detail"]
        # El mensaje tiene que explicar QUÉ pasa con el archivo y ofrecer las dos
        # salidas, no nombrar el modo técnico que el usuario eligió.
        assert _LABEL in detalle
        assert "da de alta productos" in detalle
        # La salida de dos pasos va nombrada: el replay del panel recalcula contra
        # el stock del momento, así que el objetivo SÍ es alcanzable sin
        # reestructurar el archivo. Mandar a partir las hojas como si fuera el
        # único camino es un mensaje falso.
        assert "panel de impacto" in detalle
        assert "quedar pendiente" in detalle
        assert "separá el saldo inicial" in detalle

        # Un rechazo pre-lease no deja NADA a medio importar.
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 0
        assert await _cuantas(db_session, sample_tenant, Product) == 0

    async def test_efecto_sin_hoja_no_se_descarta_en_silencio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Mapeos planos (sin `context_id`) + `inventory_effect`: 422, no silencio.

        No hay hojas contra las cuales resolver el efecto, así que `_inventory_effects`
        quedaba vacío y el import salía con el default: el usuario elegía reconstruir
        su inventario y no pasaba nada — ni el efecto ni un error.
        """
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            {
                "column_mappings": [
                    {"source_column": s, "target_field": t}
                    for s, t in (
                        ("fecha", "transaction_date"),
                        ("producto", "product_name"),
                        ("cantidad", "quantity"),
                        ("monto", "amount"),
                    )
                ],
                "confirmed_fields": {"ventas": True},
                "inventory_effect": {_CTX: "historical_replay"},
            },
        )

        assert response.status_code == 422
        assert "por hoja" in response.json()["detail"]
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 0

    async def test_el_rechazo_deja_traza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Sin `STAGE_REJECT` un 422 es indistinguible de un confirm que nunca
        llegó: diagnosticarlo obliga a reconstruir el caso desde capturas."""
        await _confirmar(client, auth_headers, archivo, _payload("historical_replay"))

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
        detail = eventos[0].detail
        assert detail is not None
        assert detail["motivo"] == "replay_no_gateable"
        assert detail["http_status"] == 422
        assert detail["context_id"] == _CTX
        assert detail["inventory_effect"] == "historical_replay"

    async def test_el_mismo_archivo_sin_replay_entra(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """La salida que el mensaje ofrece tiene que existir de verdad: el archivo
        se importa igual, sólo que sin tocar el inventario."""
        response = await _confirmar(
            client, auth_headers, archivo, _payload("informational")
        )

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 2

    async def test_sin_alta_de_productos_el_replay_se_gatea_normalmente(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El bloqueo es del archivo que carga productos, no de `historical_replay`.

        Sin `productos` confirmado no hay alta: el producto ya existe con su saldo
        y el gate corre contra él. De las dos filas entra la del 03/03 (2 unidades
        contra 2 en stock) y la del 10/03 se va a "Otros" (6 contra 0). Sin este
        caso, el bloqueo podría estar apagando el replay entero y los tests
        seguirían en verde.
        """
        db_session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                name=_PRODUCTO,
                sale_price_ars=Decimal("2100"),
                unit_cost_ars=Decimal("1200"),
                stock_units=2,
            )
        )
        await db_session.commit()

        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            _payload("historical_replay", productos=False),
        )

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 1
        assert await _cuantas(db_session, sample_tenant, UnclassifiedRecord) == 1
