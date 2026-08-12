"""F-F.3 — el confirm descuenta el stock de las ventas que acaba de importar.

Hasta F-F.2 el confirm decidía QUÉ ventas entraban (el gate cronológico) pero no
tocaba una unidad: descontar era un segundo paso manual (`/inventory-replay`),
que es la decisión de F-H3.c —confirmar → revisar → aplicar—. Esa decisión existía
por una limitación concreta: el replay no se podía validar por fecha. Desde F-F.1
sí se puede, así que el segundo clic dejó de comprar nada.

Lo que se prueba acá es lo que no se ve en el número de stock:

- que el descuento del confirm es **reversible por el borrado del archivo**, que es
  la única razón por la que se puede aplicar sin preguntar;
- que la hoja que NO aplica su historia sigue sin tocar el inventario (si esto se
  cayera, el eje por hoja habría dejado de existir sin que nadie lo declare);
- que la segunda pasada aparece **medida** en la traza (F-T) y que no se paga
  cuando no hay nada que aplicar.

La idempotencia entre el confirm y el endpoint vive en el e2e del `.xlsx`
(`test_ingestion_replay_end_to_end.py`), que es donde están los dos caminos.
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
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_CTX = "sheet:Ventas"
_PRODUCTO = "Vela aromatica 200g"
#: Lo que el producto ya tenía cargado ANTES del archivo. Que sea preexistente es
#: parte del caso: si lo creara el archivo, el borrado se lo llevaría entero y la
#: reversa del descuento no se podría distinguir de la del alta.
_STOCK_PREVIO = 10
_VENDIDAS = 4


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm/DELETE paga los reintentos de kombu. Ningún assert
    de este archivo depende del encolado."""


def _summary() -> dict[str, Any]:
    filas = [
        {
            "fecha": "2024-03-10",
            "producto": _PRODUCTO,
            "cantidad": str(_VENDIDAS),
            "monto": "8400",
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
        "preview_rows": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["fecha", "producto", "cantidad", "monto"],
                "fields": None,
                "preview_rows": filas,
                "row_count": 1,
            }
        ],
    }


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "sale",
    }


def _payload(efecto: str) -> dict[str, Any]:
    return {
        "column_mappings": [
            _map("fecha", "transaction_date"),
            _map("producto", "product_name"),
            _map("cantidad", "quantity"),
            _map("monto", "amount"),
        ],
        "confirmed_fields": {"ventas": True},
        "context_confirmed": {_CTX: True},
        "inventory_effect": {_CTX: efecto},
    }


@pytest_asyncio.fixture
async def producto(db_session: AsyncSession, sample_tenant: Tenant) -> Product:
    registro = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name=_PRODUCTO,
        sale_price_ars=Decimal("2100"),
        unit_cost_ars=Decimal("1200"),
        stock_units=_STOCK_PREVIO,
    )
    db_session.add(registro)
    await db_session.commit()
    return registro


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    registro = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas_marzo.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas_marzo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(registro)
    await db_session.commit()
    return registro


async def _confirmar(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    efecto: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json=_payload(efecto),
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _stock(db_session: AsyncSession, producto: Product) -> int:
    await db_session.refresh(producto)
    return int(producto.stock_units)


async def _movimientos_vivos(
    db_session: AsyncSession, tenant: Tenant
) -> list[InventoryMovement]:
    result = await db_session.execute(
        select(InventoryMovement).where(
            InventoryMovement.tenant_id == tenant.tenant_id,
            InventoryMovement.voided_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def _etapas_del_confirm(db_session: AsyncSession) -> dict[str, Any]:
    eventos = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "confirm")
            )
        )
        .scalars()
        .all()
    )
    assert len(eventos) == 1, f"se esperaba 1 evento 'confirm', hay {len(eventos)}"
    detail = eventos[0].detail or {}
    return dict(detail["timings_ms"]["stages"])


class TestElConfirmDescuentaYSePuedeDeshacer:
    async def test_el_borrado_del_archivo_devuelve_las_unidades(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        archivo: UploadedFile,
    ) -> None:
        """Aplicar sin preguntar sólo es defendible si se puede deshacer.

        El descuento lo escribe el confirm, pero el movimiento lleva
        `source_upload_id`, así que la reversa por procedencia (F11) lo voidea con
        todo lo demás que el archivo trajo. Y el producto tiene que quedar
        RESTAURADO, no «conservado por edición manual posterior»: si el guard del
        ledger se prendiera por el `updated_at` que mueve el descuento, borrar el
        archivo dejaría el stock bajo para siempre y `fully_reverted` diría `false`.
        """
        await _confirmar(client, auth_headers, archivo, "historical_replay")
        assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS

        movimientos = await _movimientos_vivos(db_session, sample_tenant)
        assert [(m.movement_type, int(m.qty)) for m in movimientos] == [
            ("sale", -_VENDIDAS)
        ]
        # Sin esta columna el borrado no tendría por dónde encontrarlo.
        assert movimientos[0].source_upload_id == archivo.id

        borrado = await client.delete(
            f"/api/v1/ingestion/files/{archivo.id}?confirm=true", headers=auth_headers
        )
        assert borrado.status_code == 200, borrado.text
        assert borrado.json()["fully_reverted"] is True, borrado.json()["conservados"]

        assert await _stock(db_session, producto) == _STOCK_PREVIO
        assert await _movimientos_vivos(db_session, sample_tenant) == []

    async def test_la_hoja_que_no_aplica_su_historia_no_toca_el_inventario(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        archivo: UploadedFile,
    ) -> None:
        """El control: el eje sigue siendo POR HOJA.

        Mismo archivo, misma venta, `informational`: entra a los libros y no mueve
        una unidad. Sin este caso, la segunda pasada podría estar aplicando todo lo
        que encuentra y el modo sería decorativo.
        """
        respuesta = await _confirmar(client, auth_headers, archivo, "informational")

        assert await _stock(db_session, producto) == _STOCK_PREVIO
        assert await _movimientos_vivos(db_session, sample_tenant) == []
        assert not any(
            "Se descontaron del inventario" in w for w in respuesta.get("warnings") or []
        ), respuesta.get("warnings")


class TestLaSegundaPasadaVeLoQueElImportAcabaDeEscribir:
    """Paridad con la sesión de **producción**, que va con `autoflush=False`.

    La de los tests no lo declara (se arma a mano en el conftest), así que en toda
    la suite los INSERT pendientes se flushean solos y una pasada que dependiera de
    eso pasaría igual — el mismo agujero que el importador documenta en media
    docena de lugares. Acá se apaga para que el caso corra como corre en Railway.

    **Lo que este test NO prueba:** que el `flush()` explícito del confirm sea el
    que hace la diferencia. No lo es hoy — `insert_confirmed_data` termina con un
    flush propio y sacar el del confirm no rompe nada (medido, no supuesto). Lo que
    fija es el resultado bajo la configuración real: si mañana ese flush interno se
    mueve, esto se pone rojo acá en vez de dejar de descontar en producción.
    """

    async def test_con_autoflush_apagado_el_descuento_igual_se_aplica(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        producto: Product,
        archivo: UploadedFile,
    ) -> None:
        db_session.sync_session.autoflush = False

        await _confirmar(client, auth_headers, archivo, "historical_replay")

        assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS


class TestLaSegundaPasadaSeMide:
    """F-T — la fase que midió el confirm existe para que el trabajo nuevo se vea.

    Agregar una escritura por venta adentro del confirm sin una etapa propia es
    exactamente lo que F-T vino a evitar: la próxima vez que el usuario diga que
    tarda, el desglose tiene que poder responder si es esto.
    """

    async def test_la_traza_declara_lo_que_tardo_el_descuento(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        producto: Product,
        archivo: UploadedFile,
    ) -> None:
        await _confirmar(client, auth_headers, archivo, "historical_replay")

        etapas = await _etapas_del_confirm(db_session)
        assert "replay_inventario" in etapas
        # Con denominador: un tiempo sin cuántas ventas miró no se puede comparar
        # entre dos archivos.
        assert etapas["replay_inventario"]["rows"] == 1

    async def test_sin_hojas_que_apliquen_la_etapa_no_existe(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        producto: Product,
        archivo: UploadedFile,
    ) -> None:
        """Una etapa en cero y una etapa que no corrió se leen distinto, y acá la
        diferencia importa: es lo que dice si el archivo aplicó su historia."""
        await _confirmar(client, auth_headers, archivo, "informational")

        assert "replay_inventario" not in await _etapas_del_confirm(db_session)
