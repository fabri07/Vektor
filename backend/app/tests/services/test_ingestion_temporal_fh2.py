"""F-H2: una compra futura no justifica una venta anterior.

Tres cosas distintas, que se rompen de maneras distintas:

1. **Idempotencia** — la red de seguridad. El ancla de una fila es
   ``(archivo, contexto, índice DENTRO DE SU HOJA)``, así que reordenar el
   recorrido no puede invalidarla. Es la regresión que hay que dejar clavada
   ANTES de tocar la estructura del loop de inserción, y se afirma sobre la
   huella misma —no sobre "importar dos veces no duplica"—: cualquier orden de
   recorrido, incluso uno malo, es determinístico, así que re-correr el mismo
   archivo da el mismo resultado aunque el índice se calcule sobre la cola. Lo
   que distingue un ancla buena de una mala es CONTRA QUÉ se numera la fila, y
   eso hay que mirarlo directo.

2. **Identidad independiente del orden** — una venta tiene que vincular contra
   el producto que declara una hoja de compras del mismo archivo, sin importar
   en qué solapa vino ni qué fecha tenga. F-H1 cerró esto para los catálogos;
   las compras son el otro camino por el que un producto se declara.

3. **El invariante temporal** — si la única evidencia del producto es
   POSTERIOR a la venta, la identidad se resuelve igual (la venta se importa y
   vincula), pero no se afirma que hubiera stock: se reporta
   ``historial_insuficiente_para_validar``. Es una advertencia, nunca un
   bloqueo — un negocio que arranca con mercadería y sin las facturas viejas
   tiene que poder importar su historia.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_VENTAS = "sheet:ventas"
_COMPRAS = "sheet:compras"

_MAPPINGS: dict[str, dict[str, str]] = {
    _VENTAS: {
        "fecha": "transaction_date",
        "producto": "product_name",
        "monto": "amount",
    },
    _COMPRAS: {
        "fecha": "expense_date",
        "producto": "product_name",
        "cantidad": "quantity",
        "categoria": "category",
        "monto": "amount",
    },
}
_CONFIRMED = {_VENTAS: True, _COMPRAS: True}

_PRODUCTO = "Vela aromática 200g"


def _ctx(context_id: str, entity: str, label: str, headers: list[str]) -> dict[str, Any]:
    return {
        "context_id": context_id,
        "label": label,
        "source_kind": "sheet",
        "entity_type": entity,
        "headers": headers,
        "fields": None,
        "preview_rows": [],
        "row_count": 1,
    }


def _summary(
    *,
    fecha_venta: str,
    fecha_compra: str,
    ventas_primero: bool = True,
) -> dict[str, Any]:
    """Un libro con una hoja de Ventas y una de Compras del mismo producto.

    La compra es de mercadería (categoría del vertical + cantidad), que es el
    camino por el que una compra DECLARA un producto.
    """
    ventas_ctx = _ctx(_VENTAS, "sale", "Ventas", ["fecha", "producto", "monto"])
    compras_ctx = _ctx(
        _COMPRAS,
        "expense",
        "Compras",
        ["fecha", "producto", "cantidad", "categoria", "monto"],
    )
    contexts = [ventas_ctx, compras_ctx] if ventas_primero else [compras_ctx, ventas_ctx]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "confidence": "HIGH",
        "has_venta": True,
        "has_gasto": True,
        "row_count": 2,
        "ventas_detectadas": [
            {
                "fecha": fecha_venta,
                "producto": _PRODUCTO,
                "monto": "2100",
                "__context__": _VENTAS,
            }
        ],
        "gastos_detectados": [
            {
                "fecha": fecha_compra,
                "producto": _PRODUCTO,
                "cantidad": "5",
                "categoria": "Mercadería",
                "monto": "6000",
                "__context__": _COMPRAS,
            }
        ],
        "mapping_contexts": contexts,
    }


async def _importar(
    db: AsyncSession,
    tenant: Tenant,
    summary: dict[str, Any],
    *,
    file_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        summary,
        {"ventas": True, "gastos": True},
        context_mappings=_MAPPINGS,
        context_confirmed=_CONFIRMED,
        uploaded_file_id=file_id,
    )


async def _contar(db: AsyncSession, modelo: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(modelo))).scalar_one())


@pytest.mark.asyncio
class TestIdempotenciaDelImport:
    """Red de seguridad: reordenar el recorrido no puede duplicar filas.

    El ancla vive en ``(archivo, contexto, índice en la hoja)``. Si alguna vez
    el índice pasara a ser la posición en la cola ordenada, este test se pone
    rojo — que es exactamente para lo que está.
    """

    async def test_reconfirmar_el_mismo_archivo_no_duplica(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        file_id = uuid.uuid4()
        resumen = _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05")

        await _importar(db_session, sample_tenant, resumen, file_id=file_id)
        await db_session.flush()
        ventas_1 = await _contar(db_session, SaleEntry)
        gastos_1 = await _contar(db_session, ExpenseEntry)

        await _importar(db_session, sample_tenant, resumen, file_id=file_id)
        await db_session.flush()

        assert ventas_1 == 1
        assert gastos_1 == 1
        assert await _contar(db_session, SaleEntry) == 1
        assert await _contar(db_session, ExpenseEntry) == 1

    async def test_la_huella_numera_la_fila_dentro_de_su_hoja(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El ancla, mirada de frente.

        Dos ventas y una compra: si el índice fuera la posición en el recorrido
        global, la compra tendría el 2 en vez del 0 y las huellas persistidas no
        matchearían las esperadas. Es el test que se pone rojo el día que
        alguien derive el índice de la cola ordenada (F-H3) en vez de la hoja.
        """
        file_id = uuid.uuid4()
        resumen = _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05")
        # Segunda venta: sin ella, "índice en la hoja" e "índice global" coinciden
        # y el test no distingue una implementación de la otra.
        resumen["ventas_detectadas"].append(
            {
                "fecha": "2024-03-12",
                "producto": _PRODUCTO,
                "monto": "1500",
                "__context__": _VENTAS,
            }
        )

        await _importar(db_session, sample_tenant, resumen, file_id=file_id)
        await db_session.flush()

        esperadas = {
            hashlib.sha256(
                importer._import_row_anchor(
                    sample_tenant.tenant_id, file_id, ctx_id, indice
                ).encode()
            ).hexdigest()
            for ctx_id, indice in ((_VENTAS, 0), (_VENTAS, 1), (_COMPRAS, 0))
        }
        persistidas = set(
            (
                await db_session.execute(
                    select(OperationFingerprint.fingerprint).where(
                        OperationFingerprint.tenant_id == sample_tenant.tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )

        assert esperadas <= persistidas


@pytest.mark.asyncio
class TestIdentidadDesdeCompra:
    """Una compra declara un producto; la venta lo encuentra, venga como venga."""

    async def test_la_venta_vincula_con_el_producto_que_declara_la_compra(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Hoja de Ventas primera, compra ANTERIOR: el caso fácil por fecha."""
        await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05"),
        )

        producto = (await db_session.execute(select(Product))).scalars().one()
        venta = (await db_session.execute(select(SaleEntry))).scalars().one()
        assert venta.product_id == producto.id

    async def test_vincula_aunque_la_compra_sea_posterior_a_la_venta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El caso que rompe si la identidad depende del orden de aplicación.

        La compra es del 20/03 y la venta del 10/03. Ordenar los movimientos
        por fecha manda la venta primero, y si el producto recién nace cuando
        se aplica su compra, la venta queda huérfana. La identidad tiene que
        declararse ANTES de la cola de movimientos, no durante.
        """
        await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-20"),
        )

        producto = (await db_session.execute(select(Product))).scalars().one()
        venta = (await db_session.execute(select(SaleEntry))).scalars().one()
        assert venta.product_id == producto.id


@pytest.mark.asyncio
class TestInvarianteTemporal:
    """Vincular no es afirmar que había stock."""

    async def test_compra_posterior_no_valida_la_venta_anterior(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-20"),
        )

        assert counts.get("historial_insuficiente"), (
            "una venta anterior a la única evidencia de su producto tiene que "
            "reportarse como no validable"
        )

    async def test_compra_anterior_no_levanta_la_advertencia(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: con la compra antes de la venta no hay nada que advertir.

        Sin este caso, la advertencia podría estar prendida siempre y el test
        de arriba pasaría igual.
        """
        counts = await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05"),
        )

        assert not counts.get("historial_insuficiente")
