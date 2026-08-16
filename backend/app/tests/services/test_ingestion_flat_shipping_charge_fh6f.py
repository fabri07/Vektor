"""F-H6.f — el camino plano (una sola tabla, sin `mapping_contexts`) también
cobra el envío del comprobante. Hasta acá SOLO el multi-hoja llamaba
`_cobrar_envios_de_la_hoja` — el mismo archivo real (columnas de envío) daba
resultado distinto según entrara como tabla suelta o como solapa dentro de un
`.xlsx` de varias hojas.

Dos bugs de fondo, verificados contra el código antes de arreglarlos (V24/V25
del `/code-review` sobre la rama):
  - V24: el plano llamaba `_planificar_costos_de_la_hoja(None, ...)` — la API
    arma `purchase_cost_decisions` con el `context_id` REAL (`"table"` para
    una tabla suelta, misma convención que el resto del camino plano), así
    que la búsqueda `(decisiones or {}).get(ctx_id or "")` con `None` nunca
    matcheaba: la decisión de reparto del usuario se ignoraba en silencio.
  - V25: los avisos de costo (`_avisos_costo`, ajustes ilegibles) se
    acumulaban pero nunca llegaban a `counts["avisos"]` — el multi-hoja sí lo
    hacía.

Este archivo prueba el camino plano contra el MISMO comportamiento que ya
tiene `test_ingestion_purchase_fields_fh6.py` (multi-hoja) — equivalencia
entre los dos caminos, no un comportamiento nuevo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.purchase_cost import COMPARTIDO_SUBTOTAL
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "precio_unitario": "unit_price",
    "total": "amount",
    "comprobante": "invoice_number",
    "envio": "shipping_cost",
    "proveedor": "supplier_name",
}


def _flat_summary(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Camino plano genuino: sin `multi_sheet`, sin `mapping_contexts`."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "row_count": len(filas),
        "gastos_detectados": filas,
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


def _linea_remito(
    articulo: str,
    *,
    comprobante: str = "A-0001-12345",
    envio: str = "2000",
) -> dict[str, Any]:
    return {
        "fecha": "2024-03-05",
        "articulo": articulo,
        "cantidad": "1",
        "precio_unitario": "1000",
        "total": "1000",
        "comprobante": comprobante,
        "envio": envio,
        "proveedor": "Distribuidora Sur",
    }


async def _importar(
    db: AsyncSession,
    tenant: Tenant,
    filas: list[dict[str, Any]],
    *,
    envios: str | None = None,
    purchase_cost_decisions: dict[str, PurchaseCostDecision] | None = None,
    uploaded_file_id: Any | None = None,
) -> dict[str, Any]:
    counts = await insert_confirmed_data(
        db,
        tenant.tenant_id,
        _flat_summary(filas),
        {"gastos": True},
        column_mappings=_MAPEO,
        shipping_decisions={"table": envios} if envios else None,
        purchase_cost_decisions=purchase_cost_decisions,
        uploaded_file_id=uploaded_file_id,
    )
    await db.flush()
    return counts


async def _logistica(db: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    filas = (
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                )
            )
        )
        .scalars()
        .all()
    )
    return list(filas)


class TestElCaminoPlanoCobraElEnvio:
    async def test_diez_lineas_del_mismo_remito_son_un_solo_envio(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El caso que motiva F-H6.b, ahora también en el camino plano: $2.000
        de flete, no $20.000 — antes esto NO generaba NINGÚN gasto de logística
        porque el plano nunca llamaba a `_cobrar_envios_de_la_hoja`."""
        filas = [_linea_remito(f"Articulo {i}") for i in range(10)]

        counts = await _importar(db_session, sample_tenant, filas)

        envios = await _logistica(db_session, sample_tenant)
        assert len(envios) == 1
        assert envios[0].amount == Decimal("2000.00")
        assert counts["envios"] == 1
        assert counts["envios_repetidos_colapsados"] == 1

    async def test_envio_sin_comprobante_no_se_cobra_sin_decision(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """No-invention: sin comprobante y sin decisión del usuario, dos 2.000
        son indistinguibles de un solo envío repetido — no se inventa cuál."""
        filas = [
            _linea_remito("Articulo 1", comprobante=""),
            _linea_remito("Articulo 2", comprobante=""),
        ]

        counts = await _importar(db_session, sample_tenant, filas)

        assert await _logistica(db_session, sample_tenant) == []
        assert counts["envios_sin_comprobante"] == 2

    async def test_reconfirmar_no_duplica_el_envio(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        filas = [_linea_remito(f"Articulo {i}") for i in range(3)]
        archivo_id = uuid.uuid4()

        await _importar(db_session, sample_tenant, filas, uploaded_file_id=archivo_id)
        assert len(await _logistica(db_session, sample_tenant)) == 1

        # Re-confirmar el MISMO archivo (idempotencia por ancla, que incluye el
        # `uploaded_file_id`) no debe duplicar el gasto de logística.
        await _importar(db_session, sample_tenant, filas, uploaded_file_id=archivo_id)
        assert len(await _logistica(db_session, sample_tenant)) == 1

    async def test_ajuste_ilegible_llega_a_counts_avisos(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """V25: el multi-hoja ya volcaba `_avisos_costo` a `counts["avisos"]` —
        el plano los acumulaba y nunca los exponía."""
        filas = [
            {
                "fecha": "2024-03-05",
                "articulo": "Termo acero",
                "cantidad": "1",
                "precio_unitario": "1000",
                "total": "1000",
                "descuento": "no es un número",
            }
        ]
        counts = await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _flat_summary(filas),
            {"gastos": True},
            column_mappings={
                "fecha": "expense_date",
                "articulo": "product_name",
                "cantidad": "quantity",
                "precio_unitario": "unit_price",
                "total": "amount",
                "descuento": "discount",
            },
        )
        assert counts["ajustes_ilegibles"] == 1
        assert counts.get("avisos"), "los avisos de costo no llegaron a counts"
        assert "descuento" in counts["avisos"][0]


class TestPlanoYMultiHojaConvergen:
    """El mismo input lógico entra como tabla plana o como una hoja dentro de
    un `.xlsx` multi-sección — F-H6.c: "el mismo archivo tiene que dar el mismo
    costo entre como tabla suelta o como solapa. Esa asimetría este importador
    ya la pagó dos veces" (comentario ya existente en el código, ahora cierto
    también para el envío)."""

    async def test_reparto_por_subtotal_aplica_en_el_plano(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """V24: sin el fix, la decisión de reparto se ignoraba (buscaba la
        clave `""`, la API manda `"table"`) y el costo NO se distribuía."""
        filas = [
            {
                "fecha": "2024-03-05",
                "articulo": "Termo acero",
                "cantidad": "1",
                "precio_unitario": "3000",
                "total": "3000",
                "comprobante": "B-0001",
                "envio": "1000",
                "proveedor": "Distribuidora Sur",
            },
            {
                "fecha": "2024-03-05",
                "articulo": "Yerba mate",
                "cantidad": "1",
                "precio_unitario": "1000",
                "total": "1000",
                "comprobante": "B-0001",
                "envio": "1000",
                "proveedor": "Distribuidora Sur",
            },
        ]

        await _importar(
            db_session,
            sample_tenant,
            filas,
            purchase_cost_decisions={
                "table": PurchaseCostDecision(
                    context_id="table", shared_shipping=COMPARTIDO_SUBTOTAL
                )
            },
        )

        productos = (
            (await db_session.execute(select(Product).order_by(Product.name)))
            .scalars()
            .all()
        )
        by_name = {p.name: p for p in productos}
        # $2.000 de flete repartido 75/25 por subtotal (3000 vs 1000): el termo
        # se lleva $750, la yerba $250 — sobre el precio unitario ya pagado.
        assert by_name["Termo acero"].unit_cost_ars == Decimal("3750.00")
        assert by_name["Yerba mate"].unit_cost_ars == Decimal("1250.00")
        # El flete que se repartió queda capitalizado en el stock, no como un
        # gasto de logística aparte (evita contarlo dos veces).
        envios = await _logistica(db_session, sample_tenant)
        assert len(envios) == 1
        assert envios[0].custom_fields is not None
        assert envios[0].custom_fields.get("attributed_to_inventory") is True
