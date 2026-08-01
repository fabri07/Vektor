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

from app.application.services.file_deletion_service import (
    preview_file_deletion,
    record_import_ledger,
    revert_file_data,
)
from app.domain.ingestion_version import INGESTION_VERSION
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
