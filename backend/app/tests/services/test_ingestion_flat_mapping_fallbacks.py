"""El camino de UNA sola tabla no puede perder lo que la heurística ya resolvía.

El fallback de F-H3.d.6 hizo que `column_mappings` llegue poblado en todos los
archivos de una sola tabla (la UI manda los mapeos cualificados por hoja, y antes
este camino los ignoraba). Eso destapó una clase de bug: cualquier lectura escrita
como **switch todo-o-nada** —"si hay mapeo explícito, usalo; si no, heurística"—
deja de caer a la heurística en cuanto el archivo trae un mapeo cualquiera, aunque
ese mapeo no diga nada del campo en cuestión.

El caso concreto: `unit_cost_ars` NO existe en el catálogo de `expense` (es un
target cross-entity, y `_resolve_target_cols` descarta los cross), así que en una
hoja de gastos `target_to_col` nunca lo trae. Con el switch viejo, un libro de
compras de una sola hoja perdía el costo unitario de TODAS sus líneas: margen en
cero y valuación de stock sin costo. El gemelo multi-hoja nunca tuvo el problema
porque siempre usó `or` — el mismo archivo daba resultados distintos según viniera
como hoja suelta o como solapa de un libro.

Lo mismo con la cantidad de una venta: la lectura del camino plano no caía a los
headers conocidos ni ponía piso en 1, así que una hoja con "cantidad" sin mapear
validaba el gate de `historical_replay` contra 1 unidad por venta.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_PRODUCTO = "Vela aromatica 200g"
_CTX = "table:0"


def _summary_compras() -> dict[str, Any]:
    filas = [
        {
            "fecha": "2024-03-05",
            "producto": _PRODUCTO,
            "cantidad": "5",
            "precio_compra": "1200",
            "total": "6000",
            "proveedor": "Distribuidora Sur",
        }
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": False,
        "has_gasto": True,
        "row_count": 1,
        "headers": ["fecha", "producto", "cantidad", "precio_compra", "total", "proveedor"],
        "gastos_detectados": filas,
        "preview_rows": filas,
    }


def _summary_ventas() -> dict[str, Any]:
    filas = [
        {
            "fecha": "2024-03-10",
            "producto": _PRODUCTO,
            "cantidad": "6",
            "monto": "12600",
        }
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "multi_sheet": False,
        "has_venta": True,
        "row_count": 1,
        "headers": ["fecha", "producto", "cantidad", "monto"],
        "ventas_detectadas": filas,
        "preview_rows": filas,
    }


class TestFallbacksDelCaminoPlano:
    async def test_el_costo_unitario_sobrevive_a_un_mapeo_que_no_lo_nombra(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El mapeo explícito no menciona el costo (no puede: no está en el
        catálogo de `expense`), así que la heurística tiene que seguir viéndolo."""
        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _summary_compras(),
            {"gastos": True},
            # Un mapeo realista de libro de compras: nada apunta a `unit_cost_ars`.
            column_mappings={
                "fecha": "expense_date",
                "total": "amount",
                "proveedor": "supplier_name",
            },
        )
        await db_session.flush()

        producto = (
            await db_session.execute(
                select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
            )
        ).scalar_one()
        assert producto.unit_cost_ars == Decimal("1200")

    async def test_la_cantidad_cae_a_los_headers_conocidos(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """"cantidad" sin mapear tiene que leerse igual: si no, el gate de
        `historical_replay` valida cada venta como 1 unidad y la venta se
        persiste con una cantidad que el archivo nunca dijo."""
        db_session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                name=_PRODUCTO,
                sale_price_ars=Decimal("2100"),
                unit_cost_ars=Decimal("1200"),
                stock_units=10,
            )
        )
        await db_session.flush()

        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _summary_ventas(),
            {"ventas": True},
            column_mappings={
                "fecha": "transaction_date",
                "producto": "product_name",
                "monto": "amount",
            },
        )
        await db_session.flush()

        venta = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
            )
        ).scalar_one()
        assert venta.quantity == 6

    async def test_una_cantidad_negativa_manda_la_fila_a_revision(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """E4 cambió la expectativa de este caso, a propósito.

        Antes se afirmaba que la cantidad negativa quedaba en 1: el piso evitaba
        que la fila se saltara el gate (`qty <= 0` → `continue`) y se persistiera
        con una cantidad negativa. Eso resolvía el síntoma peligroso, pero dejaba
        otro: la venta entraba con **una unidad que el archivo nunca dijo**, y era
        indistinguible de una venta legítima de una unidad — el `except: return 1`
        no separaba "la celda está vacía" de "la celda dice algo que no puedo
        usar".

        La política de E4 separa los dos: la celda vacía sigue valiendo 1 (lo
        cubre `test_la_cantidad_cae_a_los_headers_conocidos`), y la celda escrita
        e ilegible manda la fila a "Otros" con su motivo. Lo que el test viejo
        protegía sigue protegido —la fila no se persiste con cantidad negativa— y
        además no se inventa una cantidad.

        La cantidad va MAPEADA acá a propósito: sin mapeo, la lectura devuelve 1
        por el camino del valor ausente y el test no ejercería nada.
        """
        summary = _summary_ventas()
        for fila in summary["ventas_detectadas"]:
            fila["cantidad"] = "-3"
        summary["preview_rows"] = summary["ventas_detectadas"]

        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings={
                "fecha": "transaction_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
            },
        )
        await db_session.flush()

        ventas = (
            (
                await db_session.execute(
                    select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert ventas == [], (
            f"la cantidad ilegible se convirtió en una venta: "
            f"{[(v.quantity, str(v.amount)) for v in ventas]}"
        )

        capturadas = (
            (
                await db_session.execute(
                    select(UnclassifiedRecord).where(
                        UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert capturadas, "la fila no puede desaparecer: va a Otros con su motivo"
        assert "negativa" in (capturadas[0].context_label or "").lower()
