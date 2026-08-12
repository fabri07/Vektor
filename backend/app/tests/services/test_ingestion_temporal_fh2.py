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
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.inventory_effect import HISTORICAL_REPLAY
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
#: Igual, pero con la ventas mapeando `cantidad`. Desde F-F.4 es lo que separa
#: una hoja de ventas que mueve inventario de una que no habla de unidades.
_MAPPINGS_VENTA_CON_CANTIDAD: dict[str, dict[str, str]] = {
    **_MAPPINGS,
    _VENTAS: {**_MAPPINGS[_VENTAS], "cantidad": "quantity"},
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
    venta_con_cantidad: bool = False,
) -> dict[str, Any]:
    """Un libro con una hoja de Ventas y una de Compras del mismo producto.

    La compra es de mercadería (categoría del vertical + cantidad), que es el
    camino por el que una compra DECLARA un producto.
    """
    headers_ventas = ["fecha", "producto", "monto"]
    fila_venta: dict[str, Any] = {
        "fecha": fecha_venta,
        "producto": _PRODUCTO,
        "monto": "2100",
        "__context__": _VENTAS,
    }
    if venta_con_cantidad:
        headers_ventas.insert(2, "cantidad")
        fila_venta["cantidad"] = "1"
    ventas_ctx = _ctx(_VENTAS, "sale", "Ventas", headers_ventas)
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
        "ventas_detectadas": [fila_venta],
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


#: F-F.4 — el efecto que el confirm DEDUCE para este libro, no uno inventado.
#:
#: La hoja de compras identifica producto y cantidad: mercadería, mueve stock. La
#: de ventas mapea producto y monto pero **no cantidad**, así que no habla de
#: unidades y no tiene efecto — por eso no figura. Declararla igual sería armar un
#: dict que el confirm nunca produce y probar un mundo que no existe.
_EFECTOS = {_COMPRAS: HISTORICAL_REPLAY}


async def _importar(
    db: AsyncSession,
    tenant: Tenant,
    summary: dict[str, Any],
    *,
    file_id: uuid.UUID | None = None,
    inventory_effect: dict[str, str] | None = None,
    context_mappings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    return await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        summary,
        {"ventas": True, "gastos": True},
        context_mappings=context_mappings or _MAPPINGS,
        context_confirmed=_CONFIRMED,
        uploaded_file_id=file_id,
        inventory_effect=_EFECTOS if inventory_effect is None else inventory_effect,
    )


async def _contar(db: AsyncSession, modelo: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(modelo))).scalar_one())


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


class TestProyeccionDeInventario:
    """F-H3.b: el import calcula el impacto sobre el stock. F-F.4: y lo aplica.

    Que la cuenta sea correcta sirve de poco si se aplica otra cosa, y al revés.

    **La hoja de ventas de esta clase mapea `cantidad`** —a diferencia de la del
    resto del archivo— porque desde F-F.4 es lo único que hace que una hoja de
    ventas hable de unidades. Sin esa columna no habría impacto que proyectar, así
    que probarlo sobre la otra hoja mediría un archivo que la pantalla ya no
    produce.
    """

    async def test_calcula_el_impacto_de_lo_que_el_archivo_declara(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05", venta_con_cantidad=True),
            context_mappings=_MAPPINGS_VENTA_CON_CANTIDAD,
            inventory_effect={_VENTAS: HISTORICAL_REPLAY, _COMPRAS: HISTORICAL_REPLAY},
        )

        producto = (await db_session.execute(select(Product))).scalars().one()
        impacto = counts["impacto_inventario"]
        assert len(impacto) == 1
        fila = impacto[0]
        assert fila["product_id"] == str(producto.id)
        assert fila["compradas"] == 5
        assert fila["vendidas"] == 1
        # ABSOLUTOS, no `final == inicial + 4`: el saldo de apertura tiene que ser
        # el PREVIO al archivo. El producto lo crea esta misma compra, así que es
        # 0. Si la proyección se registrara DESPUÉS de `_apply_purchase_to_stock`
        # leería 5 —la compra ya aplicada— y la contaría dos veces; una aserción
        # relativa no ve esa diferencia porque los dos lados se corren juntos.
        assert fila["saldo_inicial"] == 0
        assert fila["saldo_final"] == 4

        # El stock REAL después del import: la compra suma 5. El descuento de la
        # venta lo aplica la segunda pasada del CONFIRM (F-F.3), que no corre acá
        # —esto llama al importador directo—, así que en este punto son 5.
        assert producto.stock_units == 5

    async def test_una_venta_anterior_a_su_compra_no_entra(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Vender el 10/03 lo que se compró el 20/03: la fila va a «Otros».

        Antes de F-F.4 esta venta entraba a los libros y el impacto la mostraba
        tocando -1 en el medio; el replay era opcional, así que el saldo negativo
        se reportaba y nadie lo aplicaba. Con el replay como default vuelve a regir
        la decisión de F-H3.d: **el stock no queda negativo y la fila no entra como
        venta** — se completa el inventario y se registra desde «Otros».

        Que el impacto quede sin negativos no es que se dejó de calcular: es que la
        fila que lo producía ya no está entre las ventas.
        """
        counts = await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-20", venta_con_cantidad=True),
            context_mappings=_MAPPINGS_VENTA_CON_CANTIDAD,
            inventory_effect={_VENTAS: HISTORICAL_REPLAY, _COMPRAS: HISTORICAL_REPLAY},
        )

        assert counts["ventas"] == 0
        assert counts["ventas_sin_stock"] == 1
        assert counts["stock_proyectado_negativo"] == 0
        assert await _contar(db_session, SaleEntry) == 0

    async def test_las_hojas_que_mueven_unidades_se_cuentan(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`hojas_con_replay` sale del registrador, no del payload.

        Antes daba 0 porque el replay había que pedirlo. Ahora cuenta las hojas de
        mercadería del archivo, que es lo que el confirm resolvió.
        """
        counts = await _importar(
            db_session,
            sample_tenant,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05"),
        )
        assert counts["hojas_con_replay"] == 1

    async def test_una_hoja_que_no_habla_de_inventario_no_entra_en_la_proyeccion(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Ninguna hoja declarada = ninguna hoja habla de inventario.

        F-F.4 cambió cómo se dice: antes era el modo `no_inventory` por hoja,
        ahora es la ausencia de la hoja en el dict resuelto.
        """
        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _summary(fecha_venta="2024-03-10", fecha_compra="2024-03-05"),
            {"ventas": True, "gastos": True},
            context_mappings=_MAPPINGS,
            context_confirmed=_CONFIRMED,
            inventory_effect={},
        )
        assert counts["impacto_inventario"] == []


class TestProyeccionEnArchivoDeUnaSolaHoja:
    """El hueco que casi se escapa: la proyección vivía sólo en el multi-hoja.

    `_insert_multisheet_data` corre para `inferred_type == "mixed"` o
    `multi_sheet`; un archivo de UNA hoja de ventas —el import más común que
    existe— usa el bloque inline de `_insert_confirmed_data_impl`. Calcular el
    impacto sólo para los libros de varias hojas es una funcionalidad a medias
    que nadie notaría que falta: el archivo importa bien y el aviso no aparece.
    """

    @staticmethod
    def _summary_una_hoja() -> dict[str, Any]:
        return {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "confidence": "HIGH",
            "has_venta": True,
            "row_count": 2,
            "ventas_detectadas": [
                {
                    "fecha": "2024-03-10",
                    "producto": _PRODUCTO,
                    "cantidad": "3",
                    "monto": "2100",
                },
                {
                    "fecha": "2024-03-12",
                    "producto": _PRODUCTO,
                    "cantidad": "2",
                    "monto": "1400",
                },
            ],
        }

    async def test_calcula_el_impacto_y_no_toca_el_stock(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        producto = Product(
            tenant_id=sample_tenant.tenant_id,
            name=_PRODUCTO,
            sale_price_ars=Decimal("2100"),
            unit_cost_ars=Decimal("1200"),
            stock_units=10,
        )
        db_session.add(producto)
        await db_session.flush()

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            self._summary_una_hoja(),
            {"ventas": True},
            column_mappings={
                "fecha": "transaction_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
            },
            # F-F.4: el archivo sin hojas identificadas usa `""` como contexto —
            # la clave que el recorder ya usaba para el contexto ausente.
            inventory_effect={"": HISTORICAL_REPLAY},
        )

        impacto = counts["impacto_inventario"]
        assert len(impacto) == 1
        assert impacto[0]["vendidas"] == 5
        assert impacto[0]["saldo_inicial"] == 10
        assert impacto[0]["saldo_final"] == 5
        # El stock REAL no se movió: nadie pidió el replay.
        await db_session.refresh(producto)
        assert producto.stock_units == 10

    async def test_reporta_el_negativo_proyectado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Vender 5 de un producto con 2 en stock: la historia no cierra."""
        producto = Product(
            tenant_id=sample_tenant.tenant_id,
            name=_PRODUCTO,
            sale_price_ars=Decimal("2100"),
            unit_cost_ars=Decimal("1200"),
            stock_units=2,
        )
        db_session.add(producto)
        await db_session.flush()

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            self._summary_una_hoja(),
            {"ventas": True},
            column_mappings={
                "fecha": "transaction_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
            },
            # F-F.4: el archivo sin hojas identificadas usa `""` como contexto —
            # la clave que el recorder ya usaba para el contexto ausente.
            inventory_effect={"": HISTORICAL_REPLAY},
        )

        assert counts["stock_proyectado_negativo"] == 1
        fila = counts["impacto_inventario"][0]
        assert fila["saldo_final"] == -3
        assert fila["primer_negativo_en"] == "2024-03-10"
