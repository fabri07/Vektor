"""Integral: tenant vacío → importar → confirmar → borrar → todo en cero.

Es la prueba que faltaba. Los tests unitarios verifican cada pieza por separado
(ventas, productos, maestros, stock), pero ninguno recorría el ciclo entero sobre
un archivo real con las cinco entidades a la vez. El riesgo que cubre es el de
integración: que cada paso funcione y el conjunto igual deje algo vivo.

CRITERIO DE "VACÍO" — importa la distinción:

Véktor hace **soft-delete** y conserva la trazabilidad a propósito. "Vacío" NO
significa que no queden filas en la base: significa que no queda NADA que el
usuario vea ni que entre en un cálculo. Se conservan, y está bien:

  * ventas/gastos con ``voided_at`` (todas las consultas de negocio filtran por
    ``voided_at IS NULL``),
  * productos con ``is_active=False`` — además desbloquean la reimportación,
    porque los índices únicos de identidad son PARCIALES sobre ``is_active``,
  * el archivo crudo en almacenamiento,
  * el ledger y el ``decision_audit_log``.

Por eso las aserciones cuentan lo ACTIVO, que es lo que el dashboard suma.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

pytestmark = pytest.mark.asyncio

_CTX_PROD = "sheet:precios y stock"
_CTX_VENTAS = "sheet:ventas"
_CTX_GASTOS = "sheet:gastos"


def _summary() -> dict[str, Any]:
    """Un archivo como el real: catálogo + ventas + gastos, en hojas separadas."""
    productos = [
        {
            "Productos": "Vela aromática 200g",
            "Precio de compra": "1200",
            "Precio de venta final": "2100",
            "Stock": "10",
            "__context__": _CTX_PROD,
        }
    ]
    ventas = [
        {
            "fecha": "2026-07-15",
            "detalle": "Vela aromática 200g",
            "monto": "2100",
            "cliente": "Marta Ruiz",
            "__context__": _CTX_VENTAS,
        }
    ]
    gastos = [
        {
            "fecha": "2026-07-10",
            "detalle": "Reposición",
            "monto": "1200",
            "proveedor": "Distribuidora Sur",
            "__context__": _CTX_GASTOS,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "has_venta": True,
        "has_gasto": True,
        "row_count": 3,
        "stock_detectado": productos,
        "ventas_detectadas": ventas,
        "gastos_detectados": gastos,
        "mapping_contexts": [
            {
                "context_id": _CTX_PROD,
                "label": "precios y stock",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["Productos", "Precio de compra", "Precio de venta final", "Stock"],
                "fields": None,
                "preview_rows": productos,
                "row_count": 1,
            },
            {
                "context_id": _CTX_VENTAS,
                "label": "ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["fecha", "detalle", "monto", "cliente"],
                "fields": None,
                "preview_rows": ventas,
                "row_count": 1,
            },
            {
                "context_id": _CTX_GASTOS,
                "label": "gastos",
                "source_kind": "sheet",
                "entity_type": "expense",
                "headers": ["fecha", "detalle", "monto", "proveedor"],
                "fields": None,
                "preview_rows": gastos,
                "row_count": 1,
            },
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ASTERIA_home_deco__4_.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/asteria.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=4096,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _map(source: str, target: str, ctx: str, entity: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": ctx,
        "entity_type": entity,
    }


async def _activos(db_session: AsyncSession, tenant: Tenant) -> dict[str, int]:
    """Lo que el usuario VE y lo que entra en los cálculos.

    Cuenta activo, no filas: el soft-delete conserva las anuladas a propósito
    (trazabilidad), y contarlas daría un falso negativo.
    """

    async def _n(stmt: Any) -> int:
        return int((await db_session.execute(stmt)).scalar_one())

    tid = tenant.tenant_id
    return {
        "ventas": await _n(
            select(func.count()).select_from(SaleEntry).where(
                SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None)
            )
        ),
        "gastos": await _n(
            select(func.count()).select_from(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid, ExpenseEntry.voided_at.is_(None)
            )
        ),
        "productos": await _n(
            select(func.count()).select_from(Product).where(
                Product.tenant_id == tid, Product.is_active.is_(True)
            )
        ),
        "movimientos": await _n(
            select(func.count()).select_from(InventoryMovement).where(
                InventoryMovement.tenant_id == tid,
                InventoryMovement.voided_at.is_(None),
            )
        ),
        "clientes": await _n(
            select(func.count()).select_from(Customer).where(
                Customer.tenant_id == tid, Customer.deactivated_at.is_(None)
            )
        ),
        "proveedores": await _n(
            select(func.count()).select_from(Supplier).where(
                Supplier.tenant_id == tid, Supplier.deactivated_at.is_(None)
            )
        ),
        "otros": await _n(
            select(func.count()).select_from(UnclassifiedRecord).where(
                UnclassifiedRecord.tenant_id == tid
            )
        ),
        "stock_total": int(
            (
                await db_session.execute(
                    select(func.coalesce(func.sum(Product.stock_units), 0)).where(
                        Product.tenant_id == tid, Product.is_active.is_(True)
                    )
                )
            ).scalar_one()
        ),
        "balances": int(
            (
                await db_session.execute(
                    select(func.coalesce(func.sum(InventoryBalance.current_qty), 0)).where(
                        InventoryBalance.tenant_id == tid
                    )
                )
            ).scalar_one()
        ),
    }


class TestCicloCompletoDeArchivo:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> None:
        pass

    async def test_importar_y_borrar_deja_la_cuenta_en_cero(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        archivo: UploadedFile,
        auth_headers: dict[str, Any],
    ) -> None:
        # ── 0. El tenant arranca sin datos operativos ─────────────────────────
        assert (await _activos(db_session, sample_tenant))["ventas"] == 0

        # ── 1. Confirmar el import ────────────────────────────────────────────
        confirmado = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Productos", "name", _CTX_PROD, "product"),
                    _map("Precio de compra", "unit_cost_ars", _CTX_PROD, "product"),
                    _map("Precio de venta final", "sale_price_ars", _CTX_PROD, "product"),
                    _map("Stock", "stock_units", _CTX_PROD, "product"),
                    _map("fecha", "transaction_date", _CTX_VENTAS, "sale"),
                    _map("detalle", "product_name", _CTX_VENTAS, "sale"),
                    _map("monto", "amount", _CTX_VENTAS, "sale"),
                    _map("cliente", "customer_name", _CTX_VENTAS, "sale"),
                    _map("fecha", "expense_date", _CTX_GASTOS, "expense"),
                    _map("detalle", "notes", _CTX_GASTOS, "expense"),
                    _map("monto", "amount", _CTX_GASTOS, "expense"),
                    _map("proveedor", "supplier_name", _CTX_GASTOS, "expense"),
                ],
                "confirmed_fields": {"productos": True, "ventas": True, "gastos": True},
                "context_confirmed": {
                    _CTX_PROD: True,
                    _CTX_VENTAS: True,
                    _CTX_GASTOS: True,
                },
                "stock_treatment": {_CTX_PROD: "opening_balance"},
            },
            headers=auth_headers,
        )
        assert confirmado.status_code == 200, confirmado.text

        despues_import = await _activos(db_session, sample_tenant)
        # El test no probaría nada si el import no cargó datos.
        assert despues_import["ventas"] >= 1
        assert despues_import["gastos"] >= 1
        assert despues_import["productos"] >= 1
        assert despues_import["stock_total"] > 0

        # ── 2. Borrar el archivo ──────────────────────────────────────────────
        borrado = await client.delete(
            f"/api/v1/ingestion/files/{archivo.id}?confirm=true", headers=auth_headers
        )
        assert borrado.status_code == 200, borrado.text
        cuerpo = borrado.json()
        # Sin actividad posterior, la reversión tiene que ser COMPLETA.
        assert cuerpo["fully_reverted"] is True, cuerpo["conservados"]
        assert cuerpo["conservados"] == []

        # ── 3. Nada activo, y el stock en cero ────────────────────────────────
        final = await _activos(db_session, sample_tenant)
        assert final["ventas"] == 0
        assert final["gastos"] == 0
        assert final["productos"] == 0
        assert final["movimientos"] == 0
        assert final["otros"] == 0
        assert final["stock_total"] == 0
        assert final["balances"] == 0
        # El centinela "Local" NO cuenta como cliente importado: es
        # infraestructura del tenant y tiene que sobrevivir (si no, las ventas
        # históricas quedarían sin cliente al que apuntar).
        clientes_reales = (
            (
                await db_session.execute(
                    select(Customer).where(
                        Customer.tenant_id == sample_tenant.tenant_id,
                        Customer.deactivated_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [c.name for c in clientes_reales if not c.is_sentinel] == []
        proveedores_reales = (
            (
                await db_session.execute(
                    select(Supplier).where(
                        Supplier.tenant_id == sample_tenant.tenant_id,
                        Supplier.deactivated_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [s.name for s in proveedores_reales if not s.is_sentinel] == []

        # ── 4. La trazabilidad SÍ se conserva (soft-delete, no borrado físico) ─
        ventas_totales = int(
            (
                await db_session.execute(
                    select(func.count())
                    .select_from(SaleEntry)
                    .where(SaleEntry.tenant_id == sample_tenant.tenant_id)
                )
            ).scalar_one()
        )
        assert ventas_totales >= 1, "las ventas anuladas se conservan para auditoría"
        anulada = (
            (await db_session.execute(select(SaleEntry).limit(1))).scalars().one()
        )
        assert anulada.voided_at is not None
        assert anulada.void_reason == "USER_CANCELLED"
