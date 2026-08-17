"""Borrar un archivo revierte lo que ese archivo importó.

Antes `DELETE /ingestion/files/{id}` solo hacía `deleted_at = now()`: el archivo
desaparecía de la lista y sus ventas/gastos/productos seguían en el dashboard.
Y volver a subirlo corregido duplicaba, porque las huellas anti-duplicado
incluyen el `uploaded_file_id` y un archivo nuevo no reconoce lo del anterior.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._ledger_restore import snapshot_master
from app.application.services.file_deletion_service import (
    preview_file_deletion,
    record_import_ledger,
    revert_file_data,
)
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

pytestmark = pytest.mark.asyncio


async def _archivo(
    session: AsyncSession,
    tenant: Tenant,
    *,
    version: int = INGESTION_VERSION,
) -> UploadedFile:
    """Archivo ya importado. `version` simula archivos viejos (sin ledger).

    En el flujo real, `record_import_ledger` y el sellado de `ingestion_version`
    (en `finalize_import_lease`) pasan en el MISMO confirm y la misma
    transacción; acá se arman por separado, así que el fixture lo declara.
    """
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=version,
    )
    session.add(record)
    await session.flush()
    return record


async def _producto(
    session: AsyncSession, tenant: Tenant, nombre: str, stock: int = 10
) -> Product:
    producto = Product(
        tenant_id=tenant.tenant_id,
        name=nombre,
        sale_price_ars=Decimal("100"),
        unit_cost_ars=Decimal("60"),
        stock_units=stock,
    )
    session.add(producto)
    await session.flush()
    return producto


def _detalle_producto(producto: Product, action: str) -> dict[str, Any]:
    """Forma de `insert_confirmed_data(..., return_details=True)`."""
    return {
        "action": action,
        "product_id": str(producto.id),
        "name": producto.name,
        "before": None,
        "after": {"stock_units": producto.stock_units},
    }


class TestReversaDeArchivo:
    async def test_revierte_ventas_gastos_otros_y_stock(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Jarrón azul", stock=10)

        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("5000"),
                transaction_date=datetime(2026, 1, 15, tzinfo=UTC),
                source_upload_id=archivo.id,
            )
        )
        db_session.add(
            ExpenseEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("1200"),
                transaction_date=datetime(2026, 1, 15, tzinfo=UTC),
                description="Compra de mercadería",
                category="INVENTORY",
                source_upload_id=archivo.id,
            )
        )
        db_session.add(
            UnclassifiedRecord(
                tenant_id=sample_tenant.tenant_id,
                uploaded_file_id=archivo.id,
                source="ingestion",
                row_data={"algo": "sin clasificar"},
            )
        )
        # Compra de 4 unidades que trajo el archivo: revertirla baja el stock a 6.
        db_session.add(
            InventoryMovement(
                tenant_id=sample_tenant.tenant_id,
                product_id=producto.id,
                movement_type="purchase",
                source_type="import",
                qty=4,
                source_upload_id=archivo.id,
                occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
            )
        )
        await db_session.flush()

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        assert contadores["ventas"] == 1
        assert contadores["gastos"] == 1
        assert contadores["otros"] == 1
        assert contadores["movimientos_stock"] == 1

        venta = (await db_session.execute(select(SaleEntry))).scalar_one()
        assert venta.voided_at is not None
        assert venta.void_reason == "USER_CANCELLED"

        gasto = (await db_session.execute(select(ExpenseEntry))).scalar_one()
        assert gasto.voided_at is not None

        otros = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
        assert otros == []

        # Stock revertido de forma INCREMENTAL (10 − 4), no recomputado desde el
        # ledger: no todo el stock viene de movimientos.
        await db_session.refresh(producto)
        assert producto.stock_units == 6

    async def test_desactiva_los_productos_que_el_archivo_creo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        creado = await _producto(db_session, sample_tenant, "Creado por el archivo")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[_detalle_producto(creado, "CREATED")],
        )

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(creado)
        # Desactivado, no borrado: los índices únicos de identidad de F5-B son
        # PARCIALES sobre is_active, así que esto además DESBLOQUEA reimportar.
        assert creado.is_active is False

    async def test_un_producto_preexistente_sobrevive_al_borrado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El archivo lo tocó, no lo creó: borrar el archivo no puede matarlo."""
        archivo = await _archivo(db_session, sample_tenant)
        preexistente = await _producto(db_session, sample_tenant, "Ya estaba")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            # UPDATED, no CREATED.
            product_details=[_detalle_producto(preexistente, "UPDATED")],
        )

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(preexistente)
        assert preexistente.is_active is True

    async def test_producto_con_venta_viva_de_otra_fuente_no_se_desactiva(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Una venta manual posterior lo vuelve un dato del usuario, no del archivo."""
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Vendido a mano")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[_detalle_producto(producto, "CREATED")],
        )
        # Venta SIN source_upload_id: cargada a mano, no la trajo el archivo.
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("900"),
                transaction_date=datetime(2026, 2, 1, tzinfo=UTC),
                product_id=producto.id,
            )
        )
        await db_session.flush()

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(producto)
        assert producto.is_active is True

    async def test_no_toca_datos_de_otro_archivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo_a = await _archivo(db_session, sample_tenant)
        archivo_b = await _archivo(db_session, sample_tenant)
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("777"),
                transaction_date=datetime(2026, 1, 20, tzinfo=UTC),
                source_upload_id=archivo_b.id,
            )
        )
        await db_session.flush()

        contadores = await revert_file_data(
            db_session, archivo_a.id, sample_tenant.tenant_id
        )

        assert contadores["ventas"] == 0
        venta = (await db_session.execute(select(SaleEntry))).scalar_one()
        assert venta.voided_at is None

    async def test_es_idempotente(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Revertir dos veces no re-descuenta stock ni re-cuenta nada."""
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Idempotente", stock=10)
        db_session.add(
            InventoryMovement(
                tenant_id=sample_tenant.tenant_id,
                product_id=producto.id,
                movement_type="purchase",
                source_type="import",
                qty=4,
                source_upload_id=archivo.id,
                occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
            )
        )
        await db_session.flush()

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
        segunda = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        assert segunda["movimientos_stock"] == 0
        await db_session.refresh(producto)
        assert producto.stock_units == 6


    async def test_no_borra_las_filas_de_otros_que_el_usuario_ya_clasifico(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Una fila resuelta desde /otros generó una venta/gasto REAL.

        Ese registro no lleva `source_upload_id` (lo crea `others.py`, no el
        importador), así que la reversa no lo alcanza. Borrar la fila de staging
        destruiría el único rastro que queda hacia el archivo y dejaría el dato
        derivado vivo y huérfano.
        """
        archivo = await _archivo(db_session, sample_tenant)
        db_session.add(
            UnclassifiedRecord(
                tenant_id=sample_tenant.tenant_id,
                uploaded_file_id=archivo.id,
                source="ingestion",
                row_data={"pendiente": "sí"},
                status="PENDING",
            )
        )
        db_session.add(
            UnclassifiedRecord(
                tenant_id=sample_tenant.tenant_id,
                uploaded_file_id=archivo.id,
                source="ingestion",
                row_data={"ya": "clasificada"},
                status="IMPORTED",
            )
        )
        await db_session.flush()

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        assert contadores["otros"] == 1, "solo se borra la PENDING"
        sobreviven = (
            (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
        )
        assert len(sobreviven) == 1
        assert sobreviven[0].status == "IMPORTED"

    async def test_desactiva_los_productos_creados_por_una_relectura(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Una relectura re-crea productos del archivo con su propio repair_type.

        Filtrar solo por el run de import dejaba vivos los productos de todo
        archivo releído — el mismo huérfano que este servicio existe para evitar.
        """
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Creado en la relectura")

        run = DataRepairRun(
            tenant_id=sample_tenant.tenant_id,
            repair_type="REREAD_FILE",
            status="APPLIED",
            dry_run=False,
        )
        db_session.add(run)
        await db_session.flush()
        db_session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=sample_tenant.tenant_id,
                source_file_id=archivo.id,
                product_id=producto.id,
                action="CREATE_PRODUCT",
                confidence="HIGH",
            )
        )
        await db_session.flush()

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(producto)
        assert producto.is_active is False


class TestPreviewDeBorrado:
    async def test_cuenta_lo_que_se_va_a_borrar(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("5000"),
                transaction_date=datetime(2026, 1, 15, tzinfo=UTC),
                source_upload_id=archivo.id,
            )
        )
        await db_session.flush()

        resumen = await preview_file_deletion(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        assert resumen["ventas"] == 1
        assert resumen["gastos"] == 0
        assert resumen["has_user_edits"] is False

    async def test_avisa_cuando_los_productos_no_son_rastreables(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Archivo importado ANTES del ledger: no se puede saber qué creó.

        Se informa en vez de adivinar — desactivar un producto preexistente sería
        peor que dejarlo.
        """
        archivo = await _archivo(db_session, sample_tenant, version=2)

        resumen = await preview_file_deletion(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        assert resumen["productos_no_rastreables"] is True

    async def test_con_ledger_los_productos_son_rastreables(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Con ledger")
        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[_detalle_producto(producto, "CREATED")],
        )

        resumen = await preview_file_deletion(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        assert resumen["productos_no_rastreables"] is False
        assert resumen["productos"] == 1


class TestRestauraLoQueElArchivoModifico:
    """El ledger guarda el `before` de cada `UPDATE_PRODUCT` desde siempre, y el
    borrado no lo leía: un archivo que pisaba el precio de un producto del
    usuario lo dejaba pisado para siempre."""

    async def test_devuelve_el_precio_anterior_al_borrar_el_archivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Vela aromática")

        # El archivo pisó precio y costo del producto que YA existía.
        producto.sale_price_ars = Decimal("999")
        producto.unit_cost_ars = Decimal("500")
        await db_session.flush()
        await db_session.refresh(producto)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"sale_price_ars": "100", "unit_cost_ars": "60"},
                    "after": {
                        "sale_price_ars": "999",
                        "unit_cost_ars": "500",
                        "updated_at": producto.updated_at.isoformat(),
                    },
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(producto)
        assert producto.sale_price_ars == Decimal("100")
        assert producto.unit_cost_ars == Decimal("60")
        assert contadores["productos_restaurados"] == 1
        # Modificar no es crear: el producto sigue vivo.
        assert producto.is_active is True

    async def test_un_snapshot_sin_updated_at_igual_restaura(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El bug que hacía mentir al borrado en TODO import real.

        El confirm de ingestión no sella `updated_at` en el `after` —sólo lo hace
        la relectura, que pasa `stamp_product_updated_at`—, así que la clave falta
        en todo `UPDATE_PRODUCT` que venga de un import normal. La comparación
        cruda daba `None != "2026-…"` → siempre "cambió", y el borrado marcaba
        todos los productos modificados como edición manual posterior: no
        restauraba ninguno y respondía `fully_reverted: false` diciendo algo falso.

        Los demás tests de esta clase no lo veían porque arman el ledger a mano
        CON el timestamp puesto — una forma que el confirm real no produce.
        """
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Vela de citronela")

        producto.sale_price_ars = Decimal("999")
        producto.unit_cost_ars = Decimal("500")
        await db_session.flush()
        await db_session.refresh(producto)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"sale_price_ars": "100", "unit_cost_ars": "60"},
                    # SIN `updated_at`: exactamente lo que deja el confirm real.
                    "after": {"sale_price_ars": "999", "unit_cost_ars": "500"},
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(producto)
        assert producto.sale_price_ars == Decimal("100")
        assert producto.unit_cost_ars == Decimal("60")
        assert contadores["productos_restaurados"] == 1
        assert contadores.get("productos_conservados", 0) == 0

    async def test_sin_updated_at_una_edicion_posterior_igual_frena(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: sin la señal del reloj queda la de los VALORES, que es
        evidencia directa. Si esto no frenara, el fix habría apagado el guard."""
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Difusor mimbre")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"sale_price_ars": "100"},
                    "after": {"sale_price_ars": "999"},
                }
            ],
        )
        # Alguien lo tocó después: el valor de hoy no es el que dejó el import.
        producto.sale_price_ars = Decimal("1500")
        await db_session.flush()

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(producto)
        assert producto.sale_price_ars == Decimal("1500")
        assert contadores.get("productos_restaurados", 0) == 0
        assert contadores["productos_conservados"] == 1

    async def test_una_compra_que_piso_el_costo_lo_devuelve(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El hueco que dejaban los dos niveles de test: importación REAL sobre un
        producto PREEXISTENTE, y después borrado.

        `product_details` sólo lo poblaba el camino de catálogo. Un producto
        tocado únicamente por una hoja de COMPRAS quedaba con el costo pisado para
        siempre y el DELETE respondía `fully_reverted: true` — no había item en el
        ledger, así que no había nada que conservar ni que reportar.

        Los tests de servicio fabricaban el ledger a mano (probando
        `restore_from_before`, no al importador) y el único E2E real arranca con
        el tenant vacío, sin nada que restaurar.
        """
        from app.application.services.ingestion_import_service import (
            insert_confirmed_data,
        )

        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Vela de coco")
        assert producto.unit_cost_ars == Decimal("60")

        ctx = "sheet:Compras"
        counts = await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            {
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "mapping_contexts": [
                    {
                        "context_id": ctx,
                        "label": "Compras",
                        "entity_type": "expense",
                        "source_kind": "sheet",
                        "headers": ["fecha", "articulo", "cantidad", "total", "flete_linea"],
                        "fields": None,
                        "preview_rows": [],
                        "row_count": 1,
                    }
                ],
                "gastos_detectados": [
                    {
                        "fecha": "2024-03-05",
                        "articulo": "Vela de coco",
                        "cantidad": "10",
                        "total": "1000",
                        "flete_linea": "200",
                        "__context__": ctx,
                    }
                ],
                "ventas_detectadas": [],
                "stock_detectado": [],
            },
            {"gastos": True},
            return_details=True,
            context_mappings={
                ctx: {
                    "fecha": "expense_date",
                    "articulo": "product_name",
                    "cantidad": "quantity",
                    "total": "amount",
                    "flete_linea": "shipping_cost_line",
                }
            },
            context_confirmed={ctx: True},
            uploaded_file_id=archivo.id,
            purchase_cost_decisions={
                ctx: PurchaseCostDecision(context_id=ctx, line_shipping="al_costo")
            },
        )
        await db_session.flush()
        await db_session.refresh(producto)

        # La compra pisó el costo: (1000 + 200) / 10, con el flete adentro.
        assert producto.unit_cost_ars == Decimal("120.00")
        assert (producto.custom_fields or {}).get("_vektor_costo_base") == "con_flete"

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=counts.pop("product_details", []) or [],
        )
        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(producto)
        assert producto.unit_cost_ars == Decimal("60")
        # La procedencia vuelve con el costo: si no, el guard de V5 decidiría con
        # un dato que ya no describe el número guardado.
        assert "_vektor_costo_base" not in (producto.custom_fields or {})
        assert contadores["productos_restaurados"] == 1

    async def test_no_pisa_una_edicion_posterior_del_usuario(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Si alguien tocó el producto DESPUÉS del import, su valor gana."""
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Sahumerio")

        producto.sale_price_ars = Decimal("999")
        await db_session.flush()
        await db_session.refresh(producto)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"sale_price_ars": "100"},
                    # `updated_at` capturado ANTES de la edición de abajo: el
                    # guard lo va a ver distinto del actual.
                    "after": {
                        "sale_price_ars": "999",
                        "updated_at": "2020-01-01T00:00:00+00:00",
                    },
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(producto)
        # Se conserva lo que había, NO se restaura el `before`.
        assert producto.sale_price_ars == Decimal("999")
        assert contadores["productos_restaurados"] == 0
        assert contadores["productos_conservados"] == 1

    async def test_el_stock_nunca_se_restaura_por_snapshot(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`stock_units` no es Σ(movimientos): tiene base de alta manual/chat/
        catálogo. Asignarlo desde el snapshot destruiría esa base — su reversa es
        exclusivamente el mecanismo incremental de movimientos."""
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Portarretrato", stock=7)
        await db_session.refresh(producto)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"stock_units": 999, "sale_price_ars": "100"},
                    "after": {
                        "stock_units": 7,
                        "updated_at": producto.updated_at.isoformat(),
                    },
                }
            ],
        )

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(producto)
        assert producto.stock_units == 7  # NO 999


class TestPreviewYReversaComparten:
    """El preview anticipa; el DELETE decide.

    Comparten contrato y criterio, pero el preview es read-only y ocurre ANTES:
    entre las dos llamadas alguien puede registrar una venta o editar un producto.
    Por eso se prueba que coincidan CON ESTADO SIN CAMBIOS, y que ante un cambio
    en el medio gane el resultado del DELETE.
    """

    async def test_coinciden_cuando_nada_cambia_en_el_medio(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Maceta")
        producto.sale_price_ars = Decimal("777")
        await db_session.flush()
        await db_session.refresh(producto)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"sale_price_ars": "100"},
                    "after": {
                        "sale_price_ars": "777",
                        "updated_at": producto.updated_at.isoformat(),
                    },
                }
            ],
        )

        previo = await preview_file_deletion(db_session, archivo.id, sample_tenant.tenant_id)
        assert previo["productos_a_restaurar"] == 1
        assert previo["conservados"] == []

        resultado = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
        assert resultado["productos_restaurados"] == 1
        assert resultado["conservados"] == []

    async def test_si_cambia_entre_medio_manda_el_delete(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El preview dijo "restaurable"; alguien editó el producto; el DELETE
        recalcula y lo conserva. El resultado autoritativo es el del DELETE."""
        archivo = await _archivo(db_session, sample_tenant)
        producto = await _producto(db_session, sample_tenant, "Espejo")
        producto.sale_price_ars = Decimal("777")
        await db_session.flush()
        await db_session.refresh(producto)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[
                {
                    "action": "UPDATED",
                    "product_id": str(producto.id),
                    "name": producto.name,
                    "before": {"sale_price_ars": "100"},
                    "after": {
                        "sale_price_ars": "777",
                        "updated_at": producto.updated_at.isoformat(),
                    },
                }
            ],
        )

        previo = await preview_file_deletion(db_session, archivo.id, sample_tenant.tenant_id)
        assert previo["productos_a_restaurar"] == 1

        # …y ACÁ el usuario lo edita, entre el preview y el borrado.
        producto.sale_price_ars = Decimal("555")
        await db_session.flush()
        await db_session.refresh(producto)

        resultado = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        assert resultado["productos_restaurados"] == 0
        assert resultado["productos_conservados"] == 1
        assert resultado["conservados"][0]["reasons"] == ["edicion_manual_posterior"]
        assert resultado["conservados"][0]["name"] == "Espejo"
        await db_session.refresh(producto)
        assert producto.sale_price_ars == Decimal("555")  # su edición gana

    async def test_los_protegidos_salen_con_nombre_y_motivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El backend ya calculaba los protegidos y los descartaba en silencio."""
        archivo = await _archivo(db_session, sample_tenant)
        creado = await _producto(db_session, sample_tenant, "Cuadro grande")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[_detalle_producto(creado, "CREATED")],
        )
        # Venta MANUAL posterior sobre ese producto (sin source_upload_id).
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                product_id=creado.id,
                amount=Decimal("500"),
                quantity=1,
                transaction_date=datetime.now(UTC),
                payment_method="cash",
                provenance="REAL",
            )
        )
        await db_session.flush()

        previo = await preview_file_deletion(db_session, archivo.id, sample_tenant.tenant_id)
        assert [c["name"] for c in previo["conservados"]] == ["Cuadro grande"]
        assert previo["conservados"][0]["reasons"] == ["venta_manual_posterior"]

        resultado = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
        assert [c["name"] for c in resultado["conservados"]] == ["Cuadro grande"]
        await db_session.refresh(creado)
        assert creado.is_active is True  # sobrevive: tiene ventas del usuario


async def _cliente(session: AsyncSession, tenant: Tenant, nombre: str) -> Any:
    from app.persistence.models.customer import Customer

    c = Customer(tenant_id=tenant.tenant_id, name=nombre)
    session.add(c)
    await session.flush()
    return c


class TestReversaDeMaestros:
    """Clientes y proveedores que trajo el archivo.

    Antes no se revertían: un archivo que creaba 50 clientes los dejaba vivos y
    sin manera de saber de dónde salieron.
    """

    async def test_desactiva_el_cliente_que_creo_el_archivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Carla Gómez")
        await db_session.refresh(cliente)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            master_details=[
                {
                    "action": "CREATE_CUSTOMER",
                    "kind": "customer",
                    "id": str(cliente.id),
                    "name": cliente.name,
                    "before": None,
                    "after": snapshot_master(cliente, "customer"),
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(cliente)
        assert cliente.deactivated_at is not None
        assert contadores["maestros_desactivados"] == 1

    async def test_un_cliente_con_ventas_vivas_se_conserva_y_se_informa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Desactivarlo dejaría esas ventas apuntando a una ficha inactiva."""
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Cliente con historia")
        await db_session.refresh(cliente)
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=cliente.id,
                amount=Decimal("300"),
                quantity=1,
                transaction_date=datetime.now(UTC),
                payment_method="cash",
                provenance="REAL",
            )
        )
        await db_session.flush()

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            master_details=[
                {
                    "action": "CREATE_CUSTOMER",
                    "kind": "customer",
                    "id": str(cliente.id),
                    "name": cliente.name,
                    "before": None,
                    "after": snapshot_master(cliente, "customer"),
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(cliente)
        assert cliente.deactivated_at is None
        assert contadores["maestros_desactivados"] == 0
        assert contadores["conservados"][0]["name"] == "Cliente con historia"
        assert contadores["conservados"][0]["reasons"] == ["venta_manual_posterior"]

    async def test_restaura_el_cliente_que_el_archivo_modifico(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Nombre viejo")
        await db_session.refresh(cliente)
        antes = snapshot_master(cliente, "customer")

        cliente.name = "Nombre que puso el archivo"
        await db_session.flush()
        await db_session.refresh(cliente)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            master_details=[
                {
                    "action": "UPDATE_CUSTOMER",
                    "kind": "customer",
                    "id": str(cliente.id),
                    "name": cliente.name,
                    "before": antes,
                    "after": snapshot_master(cliente, "customer"),
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(cliente)
        assert cliente.name == "Nombre viejo"
        assert cliente.deactivated_at is None  # modificar no es crear
        assert contadores["maestros_restaurados"] == 1

    async def test_el_centinela_nunca_se_desactiva(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Desactivar "Local" dejaría todas las ventas sin cliente al que apuntar."""
        from app.persistence.models._sentinel import SENTINEL_FLAG_KEY

        archivo = await _archivo(db_session, sample_tenant)
        centinela = await _cliente(db_session, sample_tenant, "Local")
        centinela.custom_fields = {SENTINEL_FLAG_KEY: "true"}
        await db_session.flush()
        await db_session.refresh(centinela)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            master_details=[
                {
                    "action": "CREATE_CUSTOMER",
                    "kind": "customer",
                    "id": str(centinela.id),
                    "name": centinela.name,
                    "before": None,
                    "after": snapshot_master(centinela, "customer"),
                }
            ],
        )

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(centinela)
        assert centinela.deactivated_at is None
        assert contadores["maestros_desactivados"] == 0


class TestReversaDeCamposCruzados:
    """F-D (7g) — un campo cross-sección (`sale→customer:last_name`, etc.) que
    el archivo escribió sobre un cliente/proveedor YA existente.

    Función HERMANA de la reversa de maestros: acá NUNCA se desactiva nada
    (F-D nunca crea entidad), sólo se restaura el campo puntual — o se
    conserva ESE campo, sin tocar el resto de la ficha, si alguien lo editó
    después del import.
    """

    async def test_restaura_el_campo_que_el_archivo_completo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Cliente Uno")
        await db_session.flush()
        await db_session.refresh(cliente)
        assert cliente.last_name is None

        cliente.last_name = "Pérez"
        await db_session.flush()

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"last_name": None},
                    "after": {
                        "last_name": "Pérez",
                        "id": str(cliente.id),
                        "kind": "customer",
                    },
                }
            ],
        )

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name is None
        assert contadores["campos_cross_restaurados"] == 1
        assert contadores["conservados"] == []

    async def test_edicion_posterior_del_campo_se_conserva_sin_tocar_el_resto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El usuario corrigió el apellido después del import — el borrado NO
        lo pisa, lo informa (`campo_modificado_posteriormente`, con
        `fields=["last_name"]`), y el resto de la ficha no se ve afectado."""
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Cliente Uno")
        await db_session.flush()
        await db_session.refresh(cliente)

        cliente.last_name = "Pérez"
        await db_session.flush()

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"last_name": None},
                    "after": {
                        "last_name": "Pérez",
                        "id": str(cliente.id),
                        "kind": "customer",
                    },
                }
            ],
        )

        # El usuario corrige el apellido A MANO, después del import.
        cliente.last_name = "Pérez Corregido"
        await db_session.flush()

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez Corregido"  # nunca se pisa
        assert cliente.name == "Cliente Uno"  # el resto de la ficha, intacto
        assert contadores["campos_cross_restaurados"] == 0
        assert len(contadores["conservados"]) == 1
        assert contadores["conservados"][0]["reasons"] == ["campo_modificado_posteriormente"]
        assert contadores["conservados"][0]["fields"] == ["last_name"]

    async def test_dos_campos_uno_editado_despues_el_otro_se_restaura(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Grano fino real: dos campos del MISMO cliente, uno editado después
        (se conserva), el otro no (se restaura) — en la MISMA entidad."""
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Cliente Uno")
        await db_session.flush()
        await db_session.refresh(cliente)

        cliente.last_name = "Pérez"
        cliente.address = "San Martín 123"
        await db_session.flush()

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"last_name": None, "address": None},
                    "after": {
                        "last_name": "Pérez",
                        "address": "San Martín 123",
                        "id": str(cliente.id),
                        "kind": "customer",
                    },
                }
            ],
        )

        cliente.last_name = "Pérez Corregido"  # el usuario corrige SOLO éste
        await db_session.flush()

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez Corregido"  # se conserva
        assert cliente.address is None  # se restaura
        assert contadores["campos_cross_restaurados"] == 1
        assert contadores["conservados"][0]["fields"] == ["last_name"]

    async def test_dos_items_del_mismo_archivo_fusionan_campos_distintos(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Dos `DataRepairItem` del MISMO archivo sobre el MISMO cliente (una
        relectura del archivo generó un segundo run), cada uno con un campo
        DISTINTO — el bug que corrigió el code-review de F-D: la reversa sólo
        miraba el `before_json` del PRIMER item y perdía el campo que sólo
        trajo el segundo. Acá los dos deben restaurar."""
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Cliente Uno")
        await db_session.flush()
        await db_session.refresh(cliente)

        cliente.last_name = "Pérez"
        await db_session.flush()

        # Primer run (import original): sólo trae `last_name`.
        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"last_name": None},
                    "after": {
                        "last_name": "Pérez",
                        "id": str(cliente.id),
                        "kind": "customer",
                    },
                }
            ],
        )

        cliente.address = "San Martín 123"
        await db_session.flush()

        # Segundo run (relectura del mismo archivo): sólo trae `address`, un
        # campo DISTINTO del primer item, sobre la MISMA entidad.
        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"address": None},
                    "after": {
                        "address": "San Martín 123",
                        "id": str(cliente.id),
                        "kind": "customer",
                    },
                }
            ],
        )

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name is None
        assert cliente.address is None
        assert contadores["campos_cross_restaurados"] == 1
        assert contadores["conservados"] == []

    async def test_el_centinela_nunca_se_toca(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        from app.application.services.customer_sentinel import (
            resolve_or_create_local_sentinel,
        )
        from app.persistence.models.customer import Customer

        archivo = await _archivo(db_session, sample_tenant)
        centinela_id = await resolve_or_create_local_sentinel(
            db_session, sample_tenant.tenant_id
        )
        centinela = await db_session.get(Customer, centinela_id)
        assert centinela is not None

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"last_name": None},
                    "after": {
                        "last_name": "No debería aplicar",
                        "id": str(centinela_id),
                        "kind": "customer",
                    },
                }
            ],
        )

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(centinela)
        assert centinela.last_name is None
        assert contadores["campos_cross_restaurados"] == 0


class TestOtraFuenteDemostrable:
    """"Otra fuente" exige evidencia real, nunca coincidencia de nombre.

    Antes esto sólo miraba ventas vivas: un producto con compras posteriores, con
    movimientos de otro origen, o traído también por otro archivo, se desactivaba
    igual y su motivo real nunca se informaba.
    """

    async def test_una_compra_posterior_lo_conserva_con_su_motivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        creado = await _producto(db_session, sample_tenant, "Difusor")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[_detalle_producto(creado, "CREATED")],
        )
        db_session.add(
            ExpenseEntry(
                tenant_id=sample_tenant.tenant_id,
                product_id=creado.id,
                amount=Decimal("200"),
                category="INVENTORY",
                description="Reposición",
                transaction_date=datetime.now(UTC),
                provenance="REAL",
            )
        )
        await db_session.flush()

        resultado = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(creado)
        assert creado.is_active is True
        assert resultado["conservados"][0]["reasons"] == ["compra_posterior"]
        # Sobrevive, pero su archivo ya no respalda los valores: hay que completar.
        assert creado.requires_completion is True

    async def test_el_propio_archivo_no_es_evidencia_de_si_mismo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Regresión: el criterio "otro archivo activo" contaba al archivo que se
        está borrando, así que no desactivaba ninguno de sus productos."""
        archivo = await _archivo(db_session, sample_tenant)
        creado = await _producto(db_session, sample_tenant, "Solo de este archivo")

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[_detalle_producto(creado, "CREATED")],
        )

        resultado = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        await db_session.refresh(creado)
        assert creado.is_active is False
        assert resultado["conservados"] == []

    async def test_otro_archivo_vivo_que_tambien_lo_creo_lo_conserva(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo_a = await _archivo(db_session, sample_tenant)
        archivo_b = await _archivo(db_session, sample_tenant)
        compartido = await _producto(db_session, sample_tenant, "En los dos archivos")

        for arch in (archivo_a, archivo_b):
            await record_import_ledger(
                db_session,
                tenant_id=sample_tenant.tenant_id,
                file_id=arch.id,
                product_details=[_detalle_producto(compartido, "CREATED")],
            )

        resultado = await revert_file_data(
            db_session, archivo_a.id, sample_tenant.tenant_id
        )

        await db_session.refresh(compartido)
        assert compartido.is_active is True
        assert resultado["conservados"][0]["reasons"] == ["otro_archivo_activo"]


class TestPreviewAnticipaMaestros:
    async def test_el_preview_no_muta_pero_anticipa_los_maestros(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El preview corre la MISMA función que la reversa, en modo lectura.

        Una implementación aparte para "anticipar" habría divergido de la que
        decide, y el preview terminaría prometiendo algo distinto de lo que pasa.
        """
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant, "Traído por el archivo")
        await db_session.refresh(cliente)

        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            master_details=[
                {
                    "action": "CREATE_CUSTOMER",
                    "kind": "customer",
                    "id": str(cliente.id),
                    "name": cliente.name,
                    "before": None,
                    "after": snapshot_master(cliente, "customer"),
                }
            ],
        )

        await preview_file_deletion(db_session, archivo.id, sample_tenant.tenant_id)
        await db_session.refresh(cliente)
        # READ-ONLY: mirar el preview no puede desactivar a nadie.
        assert cliente.deactivated_at is None

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )
        await db_session.refresh(cliente)
        assert cliente.deactivated_at is not None
        assert contadores["maestros_desactivados"] == 1


class TestOtrosYaClasificados:
    """Una fila de "Otros" que el usuario mandó a Ventas: ¿se borra con el archivo?

    Depende de si dejó procedencia. Desde F11 la venta derivada lleva
    `source_row_ref='unclassified:{id}'` y se revierte con el resto; las
    clasificadas antes nacieron huérfanas y sobreviven.
    """

    async def test_clasificada_con_procedencia_se_revierte_y_la_fila_se_borra(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        fila = UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            uploaded_file_id=archivo.id,
            source="ingestion",
            row_data={"detalle": "algo"},
            status="IMPORTED",
        )
        db_session.add(fila)
        await db_session.flush()
        # La venta que generó la clasificación, como la crea `others.py` desde F11.
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("500"),
                transaction_date=datetime.now(UTC),
                source_upload_id=archivo.id,
                source_row_ref=f"unclassified:{fila.id}",
            )
        )
        await db_session.flush()

        contadores = await revert_file_data(
            db_session, archivo.id, sample_tenant.tenant_id
        )

        venta = (await db_session.execute(select(SaleEntry))).scalar_one()
        assert venta.voided_at is not None  # el derivado se revirtió
        # Y la fila de staging se fue con él: dejarla apuntando a un archivo que
        # ya no existe no le sirve a nadie.
        assert (await db_session.execute(select(UnclassifiedRecord))).scalars().all() == []
        assert contadores["otros"] == 1
        assert not any(
            "otro_clasificado_historico" in r
            for c in contadores["conservados"]
            for r in c["reasons"]
        )

    async def test_clasificada_antes_de_f11_sobrevive_y_se_informa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Su derivado nació sin `source_upload_id`: la reversa no lo alcanza, y
        la fila es el único rastro que queda hacia el archivo."""
        archivo = await _archivo(db_session, sample_tenant)
        fila = UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            uploaded_file_id=archivo.id,
            source="ingestion",
            row_data={"detalle": "histórico"},
            status="IMPORTED",
        )
        db_session.add(fila)
        # Venta derivada SIN procedencia (como las creaba `others.py` antes).
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("300"),
                transaction_date=datetime.now(UTC),
            )
        )
        await db_session.flush()

        previo = await preview_file_deletion(
            db_session, archivo.id, sample_tenant.tenant_id
        )
        assert any(
            "otro_clasificado_historico_sin_procedencia" in c["reasons"]
            for c in previo["conservados"]
        )

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        # La venta huérfana sobrevive, y su fila también: es el único rastro.
        venta = (await db_session.execute(select(SaleEntry))).scalar_one()
        assert venta.voided_at is None
        assert len((await db_session.execute(select(UnclassifiedRecord))).scalars().all()) == 1
