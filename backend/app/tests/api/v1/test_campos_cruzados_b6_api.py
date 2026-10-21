"""B·6 (F-D) — el confirm completa la ficha del cliente desde una hoja de ventas,
y borrar el archivo la deja como estaba. Por HTTP, de punta a punta.

- Un destino cruzado que la hoja no puede escribir se rechaza con 422 ANTES del
  lease, nombrando la columna y la hoja.
- El confirm avisa cuántos datos completó; el comprobante marca la columna.
- El ledger tiene un ítem por campo, con las acciones nuevas.
- El preview del borrado y el DELETE devuelven el campo a vacío, salvo el que el
  usuario cambió después (``campo_modificado_posteriormente``).
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.import_receipt import build_import_receipt
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant
from app.tests._respaldo import productos_con_compra

pytestmark = pytest.mark.asyncio

_CTX = "sheet:Ventas"
_DNI = "30111222"
_HEADERS = ["Fecha", "Monto", "Producto", "Documento", "Apellido", "Domicilio"]


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def _summary() -> dict[str, Any]:
    filas = [
        {
            "Fecha": "2024-03-15",
            "Monto": "1500",
            "Producto": "Vela",
            "Documento": _DNI,
            "Apellido": "Pérez",
            "Domicilio": "Mitre 10",
            "__context__": _CTX,
        }
    ]
    return {
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
                "label": "Ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": filas,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    await productos_con_compra(db_session, sample_tenant.tenant_id, ["Vela"])
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key="uploads/test/uuid/ventas.xlsx",
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


@pytest_asyncio.fixture
async def cliente(db_session: AsyncSession, sample_tenant: Tenant) -> Customer:
    c = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni=_DNI)
    db_session.add(c)
    await db_session.commit()
    return c


def _m(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "sale",
    }


def _mapeo(**cruzados: str) -> list[dict[str, Any]]:
    base = [
        _m("Fecha", "transaction_date"),
        _m("Monto", "amount"),
        _m("Producto", "product_name"),
        _m("Documento", "customer_dni"),
    ]
    return base + [_m(col, target) for col, target in cruzados.items()]


async def _confirmar(
    client: AsyncClient, h: dict[str, Any], archivo: UploadedFile, mapeo: list[dict[str, Any]]
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": mapeo,
            "confirmed_fields": {"ventas": True},
            "context_confirmed": {_CTX: True},
        },
        headers=h,
    )


class TestRechazoAntesDelLease:
    @pytest.mark.parametrize(
        "target",
        [
            # Ya es canónico de la venta (`customer_name`): dos rutas al mismo dato.
            "customer:name",
            # Prohibido en cualquier ruta cruzada.
            "product:stock_units",
            # Destino de una compra, no de una venta.
            "supplier:payment_method",
            # Fuera de B·6: toca el motor de costo.
            "product:unit_cost_ars",
        ],
    )
    async def test_destino_cruzado_que_la_hoja_no_escribe_es_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        target: str,
    ) -> None:
        r = await _confirmar(client, auth_headers, archivo, _mapeo(Apellido=target))

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "«Ventas»" in detalle
        assert "«Apellido»" in detalle
        assert target in detalle

    async def test_dos_columnas_al_mismo_campo_de_la_ficha_es_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        r = await _confirmar(
            client,
            auth_headers,
            archivo,
            _mapeo(Apellido="customer:address", Domicilio="customer:address"),
        )

        assert r.status_code == 422, r.text
        detalle = r.json()["detail"]
        assert "«Ventas»" in detalle
        assert "Apellido" in detalle and "Domicilio" in detalle

    async def test_el_rechazo_no_toma_el_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        r = await _confirmar(client, auth_headers, archivo, _mapeo(Apellido="customer:name"))
        assert r.status_code == 422
        await db_session.refresh(archivo)
        assert archivo.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION


class TestConfirmYBorrado:
    async def test_completa_la_ficha_avisa_y_el_borrado_la_deja_como_estaba(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        cliente: Customer,
        db_session: AsyncSession,
    ) -> None:
        r = await _confirmar(
            client,
            auth_headers,
            archivo,
            _mapeo(Apellido="customer:last_name", Domicilio="customer:address"),
        )
        assert r.status_code == 200, r.text
        avisos = " ".join(r.json()["warnings"])
        assert "Se completaron 2 dato(s) en 1 ficha(s)" in avisos

        await db_session.refresh(cliente)
        assert (cliente.last_name, cliente.address) == ("Pérez", "Mitre 10")
        items = (
            await db_session.execute(
                select(DataRepairItem).where(DataRepairItem.source_file_id == archivo.id)
            )
        ).scalars().all()
        assert sorted(i.action for i in items if "CROSS" in i.action) == [
            "UPDATE_CUSTOMER_CROSS_FIELD",
            "UPDATE_CUSTOMER_CROSS_FIELD",
        ]

        # El usuario corrige el domicilio a mano, después del import.
        r = await client.patch(
            f"/api/v1/customers/{cliente.id}",
            json={"address": "Mitre 1050"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text

        preview = await client.get(
            f"/api/v1/ingestion/files/{archivo.id}/deletion-preview", headers=auth_headers
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["campos_cruzados_a_restaurar"] == 1
        conservados = [
            c for c in preview.json()["conservados"] if c["entity_type"] == "customer"
        ]
        assert [(c["reasons"], c["fields"]) for c in conservados] == [
            (["campo_modificado_posteriormente"], ["address"])
        ]

        r = await client.delete(
            f"/api/v1/ingestion/files/{archivo.id}?confirm=true", headers=auth_headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["restored"]["cross_fields"] == 1
        assert r.json()["fully_reverted"] is False

        await db_session.refresh(cliente)
        assert cliente.last_name is None
        assert cliente.address == "Mitre 1050"

    async def test_el_comprobante_marca_la_columna_de_la_ficha(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        cliente: Customer,
    ) -> None:
        r = await _confirmar(
            client, auth_headers, archivo, _mapeo(Apellido="customer:last_name")
        )
        assert r.status_code == 200, r.text

        r = await client.get(f"/api/v1/ingestion/files/{archivo.id}/receipt", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert "Completa la ficha del cliente" in r.text


class TestComprobante:
    def _ctx(self) -> list[dict[str, Any]]:
        return [{"context_id": _CTX, "label": "Ventas", "headers": ["A", "I"], "row_count": 1}]

    def test_confirm_guardado_o_transformado_con_motivo(self) -> None:
        entradas = build_import_receipt(
            contexts=self._ctx(),
            effective_mapping={_CTX: {"A": "customer:last_name", "I": "customer:iva_condition"}},
            campos_cruzados_aplicados=True,
        )
        por_col = {e.source_column: e for e in entradas}
        assert por_col["A"].result == "guardado"
        assert por_col["I"].result == "transformado"
        assert por_col["A"].reason is not None
        assert "sólo si ese campo estaba vacío" in por_col["A"].reason

    def test_la_relectura_no_dice_guardado(self) -> None:
        entradas = build_import_receipt(
            contexts=self._ctx(),
            effective_mapping={_CTX: {"A": "customer:last_name", "I": "supplier:name"}},
        )
        por_col = {e.source_column: e for e in entradas}
        assert por_col["A"].result == "excluido"
        # `supplier:name` es del catálogo de productos: se guarda igual.
        assert por_col["I"].result == "guardado"
