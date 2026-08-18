"""F-H6.c — aceptación: todo target monetario tiene un consumidor que mueve el costo.

Se escribieron ROJAS a propósito, antes de implementar F-H6.c, con
`xfail(strict=True)`. Las marcas se sacaron al cablear el importador: el `strict`
las habría reportado como error en cuanto pasaran, que es justo el ratchet que se
buscaba — no se puede implementar la fase y "olvidarse" de que ya están verdes.

F-M.7 agregó `discount`, `taxes` y `shipping_cost_line` al catálogo: el usuario ya
puede elegirlos en la pantalla de mapeo. Pero el importador todavía no lee sus
valores, así que una columna mapeada a «Descuento» hoy no mueve ningún número —
un no-op silencioso, que es peor que no ofrecer el campo.

El criterio que fijan estos tests, y que es el que decide si la rama es
entregable:

> Todo target monetario que el catálogo ofrece tiene un consumidor **probado** que
> modifica el costo, o la confirmación lo rechaza.

Apuntan al importador REAL (`insert_confirmed_data`) y miran lo que quedó
PERSISTIDO, no el resultado de `build_line_costs`: la aritmética ya está probada
en `test_purchase_cost.py`, y lo que acá se prueba es justamente lo que falta —
que alguien la llame con los valores del archivo.

Uno de los seis (el flete del comprobante que se cobra una vez) YA pasa: es
F-H6.b, y está incluido como control. Si ese se pusiera rojo, el problema no
sería F-H6.c sino una regresión.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_CTX = "sheet:Compras"
_PRODUCTO = "Vela aromatica 200g"

_HEADERS = [
    "fecha",
    "articulo",
    "cantidad",
    "precio_unitario",
    "total",
    "descuento",
    "iva",
    "flete_linea",
    "envio",
    "comprobante",
    "proveedor",
]

_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "precio_unitario": "unit_price",
    "total": "amount",
    "descuento": "discount",
    "iva": "taxes",
    "flete_linea": "shipping_cost_line",
    "envio": "shipping_cost",
    "comprobante": "invoice_number",
    "proveedor": "supplier_name",
}


def _fila(**over: Any) -> dict[str, Any]:
    """Una línea de compra: 10 unidades a 1200 = 12000."""
    return {
        "fecha": "2024-03-05",
        "articulo": _PRODUCTO,
        "cantidad": "10",
        "precio_unitario": "1200",
        "total": "12000",
        "descuento": "0",
        "iva": "0",
        "flete_linea": "0",
        "envio": "0",
        "comprobante": "A-0001",
        "proveedor": "Distribuidora Sur",
        **over,
    }


def _summary(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Compras",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": [],
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": [{**f, "__context__": _CTX} for f in filas],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


async def _importar(
    db: AsyncSession,
    tenant: Tenant,
    filas: list[dict[str, Any]],
    *,
    costos: dict[str, Any] | None = None,
    mapeo: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Corre el importador real. `costos` es la decisión de F-H6.c para la hoja.

    El kwarg se pasa SÓLO cuando hay decisión, a propósito: así los casos que no
    la necesitan corren contra el importador de hoy y fallan por lo que de verdad
    falta —el valor mapeado que no mueve nada, el aviso que no llega— en vez de
    por un `TypeError` que taparía la diferencia entre «no está cableado» y
    «está cableado y lo ignora».
    """
    extra: dict[str, Any] = {}
    if costos is not None:
        # El MISMO tipo que arma el endpoint al confirmar: si el test construyera
        # un dict suelto probaría una forma que la API no produce.
        extra["purchase_cost_decisions"] = {
            _CTX: PurchaseCostDecision(context_id=_CTX, **costos)
        }
    counts = await insert_confirmed_data(
        db,
        tenant.tenant_id,
        _summary(filas),
        {"gastos": True},
        context_mappings={_CTX: mapeo or _MAPEO},
        context_confirmed={_CTX: True},
        **extra,
    )
    await db.flush()
    return counts


async def _costo_del_producto(db: AsyncSession, tenant: Tenant) -> Decimal:
    """El costo de REFERENCIA con el que quedó el producto: lo que costó de verdad.

    Desde F-H6.d los dos observables se separaron y cada test tiene que decir cuál
    mira. Acá vive el costo final —monto ajustado por descuento e impuestos, más
    el flete que se haya capitalizado o repartido—, que es lo que el negocio pagó
    por la mercadería y contra lo que se calcula el margen.
    """
    prods = (
        (
            await db.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(prods) == 1, f"se esperaba 1 producto, hay {len(prods)}"
    assert prods[0].unit_cost_ars is not None
    return Decimal(str(prods[0].unit_cost_ars))


async def _costo_del_movimiento(db: AsyncSession, tenant: Tenant) -> Decimal:
    """Lo que FACTURÓ el proveedor en esta compra.

    Antes de F-H6.d este helper devolvía el costo final, porque los callers
    pisaban `unit_cost` con él antes de llegar al movimiento — y con eso se perdía
    el precio de la factura, que no vive en ningún otro lado. Ahora el movimiento
    guarda el renglón del comprobante y el producto guarda el costo real; el
    fallback sólo aparece cuando el archivo no declara uno de los dos.
    """
    movs = (
        (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.tenant_id == tenant.tenant_id,
                    InventoryMovement.movement_type == "purchase",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(movs) == 1, f"se esperaba 1 movimiento de compra, hay {len(movs)}"
    assert movs[0].unit_cost is not None
    return Decimal(str(movs[0].unit_cost))


async def _gastos(db: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )


class TestElDescuentoYLosImpuestosLleganAlCosto:
    async def test_un_descuento_declarado_baja_el_costo_final(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """12000 − 2000 sobre 10 unidades = 1000, no 1200."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(descuento="2000")],
            costos={"base": "monto_sin_ajustes"},
        )
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1000.00")
        # …y el movimiento sigue diciendo lo que facturó el proveedor.
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")

    async def test_un_impuesto_declarado_sube_el_costo_final(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """12000 + 500 sobre 10 unidades = 1250."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(iva="500")],
            costos={"base": "monto_sin_ajustes"},
        )
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1250.00")
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")

    async def test_sin_declarar_la_base_el_monto_se_toma_final_pero_se_avisa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El default es seguro —no cambia números que el usuario no pidió cambiar—
        pero no puede ser mudo: mapear un descuento y que no pase nada, sin decirlo,
        es el no-op silencioso que esta fase viene a eliminar."""
        counts = await _importar(db_session, sample_tenant, [_fila(descuento="2000")])

        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")
        avisos = counts.get("avisos") or counts.get("warnings") or []
        assert any("descuento" in str(a).lower() for a in avisos), (
            f"mapear un descuento sin declarar la base tiene que avisar; counts={counts}"
        )


class TestLosDosFletesNoSonElMismoCargo:
    async def test_el_flete_de_linea_se_suma_a_cada_fila(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Ya viene repartido por quien armó la planilla: (12000 + 300) / 10."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(flete_linea="300")],
            costos={"line_shipping": "al_costo"},
        )
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1230.00")
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")

    async def test_el_flete_del_comprobante_se_cobra_una_sola_vez(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """CONTROL — esto es F-H6.b y ya pasa. Dos filas del mismo comprobante
        repiten la cifra del envío: es UN cargo, no dos. Si este test se pone
        rojo, hay una regresión, no una feature faltante."""
        await _importar(
            db_session,
            sample_tenant,
            [
                _fila(envio="500", articulo="Vela aromatica", total="6000", cantidad="5"),
                _fila(envio="500", articulo="Taza ceramica", total="6000", cantidad="5"),
            ],
        )
        fletes = [
            g
            for g in await _gastos(db_session, sample_tenant)
            if (g.category or "").upper() == "LOGISTICS"
        ]
        assert len(fletes) == 1, "la cifra repetida es un solo cargo"
        assert Decimal(str(fletes[0].amount)) == Decimal("500")

    async def test_los_dos_fletes_conviven_sin_mezclarse(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Semánticas opuestas en la misma hoja: el del comprobante viene repetido
        y se cobra UNA vez; el de línea es el pedazo de cada artículo y se suma.
        Fusionarlos repartiría algo que ya venía repartido.

        Desde F-H6.e los DOS dejan un gasto —el de línea también, que antes se
        capitalizaba sin que la plata saliera de ningún lado— y por eso acá hay
        dos cargos y no uno. Lo que sigue siendo cierto, y es lo que este test
        cuida, es que no se mezclan: cada uno con su importe y su ancla.
        """
        await _importar(
            db_session,
            sample_tenant,
            [_fila(envio="500", flete_linea="300")],
            costos={"line_shipping": "al_costo"},
        )
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1230.00")
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")
        fletes = sorted(
            Decimal(str(g.amount))
            for g in await _gastos(db_session, sample_tenant)
            if (g.category or "").upper() == "LOGISTICS"
        )
        assert fletes == [Decimal("300"), Decimal("500")]


class TestUnValorInvalidoNoEscribeNada:
    async def test_un_modo_desconocido_no_deja_datos_a_medio_importar(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`build_line_costs` levanta ValueError ante un modo desconocido en vez de
        caer a «no cobrar» en silencio. Eso tiene que cortar ANTES de escribir: un
        import a medias es peor que uno rechazado."""
        with pytest.raises(ValueError):
            await _importar(
                db_session,
                sample_tenant,
                [_fila(descuento="2000")],
                costos={"base": "modo_que_no_existe"},
            )
        assert await _gastos(db_session, sample_tenant) == []
        productos = (
            (
                await db_session.execute(
                    select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert productos == []


def _summary_plano(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """El MISMO archivo, pero como tabla suelta en vez de solapa.

    Sin `mapping_contexts` el importador toma el camino plano, que calcula el
    costo en otro lugar del código. Que los dos den lo mismo no es un detalle:
    este importador ya pagó dos veces que un camino aprendiera algo y el otro no.
    """
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "headers": _HEADERS,
        "gastos_detectados": filas,
        "ventas_detectadas": [],
        "stock_detectado": [],
        "row_count": len(filas),
    }


class TestElCaminoPlanoDaElMismoCosto:
    async def test_un_descuento_declarado_tambien_baja_el_costo_en_una_tabla_suelta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _summary_plano([_fila(descuento="2000")]),
            {"gastos": True},
            column_mappings=_MAPEO,
            # F-H6.f (V24): la clave del camino plano es `"table"` — la misma
            # que usa `api/v1/ingestion.py` para el `context_id` sintético de
            # una tabla suelta, no `""`.
            purchase_cost_decisions={
                "table": PurchaseCostDecision(context_id="table", base="monto_sin_ajustes")
            },
        )
        await db_session.flush()
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1000.00")

    async def test_y_el_flete_de_linea_tambien_se_capitaliza(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _summary_plano([_fila(flete_linea="300")]),
            {"gastos": True},
            column_mappings=_MAPEO,
            # F-H6.f (V24): ver el comentario del test anterior.
            purchase_cost_decisions={
                "table": PurchaseCostDecision(context_id="table", line_shipping="al_costo")
            },
        )
        await db_session.flush()
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1230.00")
        # Acá los dos valen lo mismo, y es correcto: el camino plano lee el costo
        # facturado de `unit_cost_ars`/heurística, no de `unit_price`, así que
        # este archivo no declara un precio de factura. Sin ese dato el movimiento
        # cae al costo final, que es el único número que hay sobre la compra.
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1230.00")


class TestUnaCeldaQueNoSePudoLeerSeCuentaYSeAvisa:
    """«ver factura» en la columna de descuento no es «sin descuento».

    El import no se cae —la fila tiene monto y es una compra válida— pero el
    usuario tiene que enterarse de que ese ajuste no se aplicó. Tratarlo como
    cero en silencio es perder un dato sin que nadie lo note.
    """

    async def test_la_fila_entra_pero_el_ajuste_se_reporta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar(
            db_session,
            sample_tenant,
            [_fila(descuento="ver factura")],
            costos={"base": "monto_sin_ajustes"},
        )

        # La compra entró, con el monto sin ajustar: 12000 / 10.
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")
        assert counts.get("ajustes_ilegibles") == 1
        avisos = counts.get("avisos") or []
        assert any("descuento" in a.lower() for a in avisos), (
            f"el aviso tiene que nombrar la columna; avisos={avisos}"
        )
        assert any("no se pudieron leer" in a for a in avisos)

    async def test_una_celda_vacia_no_genera_ningun_aviso(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La contracara: vacío significa «esta fila no tiene descuento», que es
        un dato normal y no merece ruido."""
        counts = await _importar(
            db_session,
            sample_tenant,
            [_fila(descuento="")],
            costos={"base": "monto_sin_ajustes"},
        )
        assert counts.get("ajustes_ilegibles", 0) == 0
        assert not [a for a in (counts.get("avisos") or []) if "no se pudieron leer" in a]


async def _costos_por_producto(
    db: AsyncSession, tenant: Tenant
) -> dict[str, Decimal]:
    """El costo de referencia con el que quedó cada producto de la compra."""
    productos = (
        (
            await db.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        str(p.name): Decimal(str(p.unit_cost_ars))
        for p in productos
        if p.unit_cost_ars is not None
    }


def _linea_de(articulo: str, total: str, **over: Any) -> dict[str, Any]:
    """Una línea de UNA unidad, para que el costo unitario sea el total."""
    return _fila(articulo=articulo, cantidad="1", precio_unitario=total, total=total, **over)


async def _procedencia_del_costo(db: AsyncSession, tenant: Tenant) -> str | None:
    """Qué declara el producto sobre su propio costo: si incluye flete o no."""
    prods = (
        (
            await db.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(prods) == 1, f"se esperaba 1 producto, hay {len(prods)}"
    valor = (prods[0].custom_fields or {}).get("_vektor_costo_base")
    return str(valor) if valor is not None else None


class TestElProductoDeclaraSiSuCostoIncluyeFlete:
    """F-H6.d — sin procedencia, comparar dos costos es comparar cosas distintas.

    Un costo de 110 con el flete adentro y uno de 100 facturado describen la misma
    compra, y el segundo no es más barato. El guard de V5 decide con este dato si
    una compra nueva puede pisar el costo guardado; por eso la procedencia se
    escribe en la MISMA operación que el costo y no después.
    """

    async def test_con_flete_capitalizado_lo_declara(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _importar(
            db_session,
            sample_tenant,
            [_fila(flete_linea="300")],
            costos={"line_shipping": "al_costo"},
        )
        assert await _procedencia_del_costo(db_session, sample_tenant) == "con_flete"

    async def test_con_el_envio_repartido_tambien(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _importar(
            db_session,
            sample_tenant,
            [_fila(envio="500")],
            costos={"shared_shipping": "por_subtotal"},
        )
        assert await _procedencia_del_costo(db_session, sample_tenant) == "con_flete"

    async def test_sin_flete_en_el_costo_tambien_se_declara(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Hubo cálculo de costo y el flete NO entró: es una afirmación, no un
        hueco. `gasto_aparte` deja el envío afuera del costo, a propósito."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(flete_linea="300")],
            costos={"line_shipping": "gasto_aparte"},
        )
        assert await _procedencia_del_costo(db_session, sample_tenant) == "sin_flete"

    async def test_sin_columnas_de_costo_no_se_inventa_procedencia(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La ausencia de la clave significa «no sé», que NO es «sin flete». Un
        archivo que no trae ninguna columna de ajuste no permite afirmar nada
        sobre qué contiene el precio que declara."""
        mapeo_pelado = {
            "fecha": "expense_date",
            "articulo": "product_name",
            "cantidad": "quantity",
            "precio_unitario": "unit_price",
            "total": "amount",
        }
        await _importar(db_session, sample_tenant, [_fila()], mapeo=mapeo_pelado)
        assert await _procedencia_del_costo(db_session, sample_tenant) is None


class TestElEnvioCapitalizadoNoSeCuentaDosVeces:
    """F-H6.d — el corte es resultado vs caja, y elegir mal da un número que miente.

    $100 de mercadería + $10 de flete, mismo comprobante:

        no distribuir  → gastos 110 · stock 100    el 10 vive en el resultado
        por subtotal   → gastos 100 · stock 110    el 10 vive en el activo
        por subtotal, sin el filtro
                       → gastos 110 · stock 110    el 10 vive en los dos

    La tercera fila es el doble conteo que este filtro cierra, y sólo aparece
    desde que el reparto reparte de verdad.
    """

    async def _gastos_del_resultado(
        self, db: AsyncSession, tenant: Tenant
    ) -> Decimal:
        from app.persistence.repositories.transaction_repository import (
            ExpenseRepository,
        )

        return Decimal(str(await ExpenseRepository(db).total_expenses(tenant.tenant_id)))

    async def _plata_que_salio(self, db: AsyncSession, tenant: Tenant) -> Decimal:
        """Suma cruda, sin filtro: la vista de CAJA."""
        return sum(
            (Decimal(str(g.amount)) for g in await _gastos(db, tenant)),
            Decimal("0"),
        )

    async def test_repartido_el_flete_sale_del_resultado_pero_no_de_la_caja(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _importar(
            db_session,
            sample_tenant,
            [_linea_de("Vela aromatica", "100", envio="10")],
            costos={"shared_shipping": "por_subtotal"},
        )

        # El 10 está adentro del costo del producto…
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("110")
        # …así que el resultado no lo cuenta otra vez.
        assert await self._gastos_del_resultado(db_session, sample_tenant) == Decimal("100")
        # Pero la plata salió: el gasto existe y la caja lo ve. Sin esto, el
        # arqueo no cuadraría con lo que hay en el cajón, y la reversa del
        # archivo no tendría qué revertir.
        assert await self._plata_que_salio(db_session, sample_tenant) == Decimal("110")

    async def test_sin_repartir_el_flete_es_gasto_del_periodo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control con el default: acá el 10 NO está en el stock, así que tiene
        que estar en el resultado. Si el filtro lo sacara también en este caso,
        el gasto desaparecería de los dos lados."""
        await _importar(
            db_session,
            sample_tenant,
            [_linea_de("Vela aromatica", "100", envio="10")],
        )

        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("100")
        assert await self._gastos_del_resultado(db_session, sample_tenant) == Decimal("110")

    async def test_un_grupo_que_pidio_reparto_y_no_pudo_sigue_siendo_gasto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La marca es el HECHO CONSUMADO, no la intención. Sin comprobante el
        reparto no ocurre, así que ese flete sigue siendo gasto del período —
        marcarlo por lo que el usuario pidió lo borraría de los dos lados."""
        await _importar(
            db_session,
            sample_tenant,
            [_linea_de("Vela aromatica", "100", envio="10", comprobante="")],
            costos={"shared_shipping": "por_subtotal"},
        )

        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("100")
        # El envío sin comprobante no se cobra (F-H6.b, no-invention), así que
        # acá el resultado son sólo los 100 de mercadería.
        assert await self._gastos_del_resultado(db_session, sample_tenant) == Decimal("100")


class TestUnaCompraNuevaNoPisaUnCostoQueIncluiaFlete:
    """V5, end-to-end. El mismo producto entra dos veces con formatos distintos.

    Primero con el flete capitalizado (1230) y después con una factura que sólo
    declara el renglón (1100). Antes la segunda pisaba a la primera y el costo
    "bajaba" sin que nada se abaratara: cambió el formato de la planilla.
    """

    async def test_el_costo_con_flete_sobrevive_a_una_factura_sin_flete(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _importar(
            db_session,
            sample_tenant,
            [_fila(flete_linea="300")],
            costos={"line_shipping": "al_costo"},
        )
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1230.00")

        await _importar(
            db_session,
            sample_tenant,
            [
                _fila(
                    fecha="2024-04-05",
                    comprobante="A-0002",
                    precio_unitario="1100",
                    total="11000",
                )
            ],
        )

        # No se pisó: 1100 facturado no es más barato que 1230 con flete adentro.
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1230.00")
        # …y la procedencia tampoco, que si no describirían cosas distintas.
        assert await _procedencia_del_costo(db_session, sample_tenant) == "con_flete"
        # El precio de ESTA compra no se perdió: vive en su movimiento.
        movs = (
            (
                await db_session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == sample_tenant.tenant_id,
                        InventoryMovement.movement_type == "purchase",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(Decimal(str(m.unit_cost)) for m in movs) == [
            Decimal("1100.00"),
            Decimal("1200.00"),
        ]

    async def test_un_producto_sin_costo_si_recibe_el_de_la_compra(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control obligatorio: sin esto el guard apagaría la carga inicial de
        costos y el stock quedaría valuado en cero."""
        await _importar(db_session, sample_tenant, [_fila()])
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1200.00")


class TestElFleteDeLineaSaleDeLaCaja:
    """F-H6.e — el flete asignado a la línea nunca generaba un gasto.

    Con `al_costo` subía `unit_cost_ars` y el dinero no salía de ningún lado: un
    asiento que no cierra, con el activo inflado contra nada. Con `gasto_aparte`
    —el default— era un no-op puro, aunque el nombre del modo prometiera un
    gasto. Los dos targets de envío ahora tienen contrapartida.
    """

    async def test_al_costo_capitaliza_y_ademas_registra_la_salida(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _importar(
            db_session,
            sample_tenant,
            [_fila(flete_linea="300")],
            costos={"line_shipping": "al_costo"},
        )

        # Sigue entrando al costo: 12000 + 300 sobre 10 unidades.
        assert await _costo_del_producto(db_session, sample_tenant) == Decimal("1230.00")
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")
        # …y ahora también sale de la caja.
        logistica = [
            g for g in await _gastos(db_session, sample_tenant) if g.category == "LOGISTICS"
        ]
        assert len(logistica) == 1
        assert Decimal(str(logistica[0].amount)) == Decimal("300.00")
        # Marcado, para que los agregados de resultado no lo cuenten dos veces:
        # ese importe ya está adentro del valor del stock.
        assert (logistica[0].custom_fields or {}).get("attributed_to_inventory") is True

    async def test_gasto_aparte_registra_el_gasto_y_no_toca_el_costo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El default. Antes no hacía ninguna de las dos cosas."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(flete_linea="300")],
            costos={"line_shipping": "gasto_aparte"},
        )

        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1200.00")
        logistica = [
            g for g in await _gastos(db_session, sample_tenant) if g.category == "LOGISTICS"
        ]
        assert len(logistica) == 1
        assert Decimal(str(logistica[0].amount)) == Decimal("300.00")
        # No se capitalizó: el agregado de resultado SÍ tiene que verlo.
        assert not (logistica[0].custom_fields or {}).get("attributed_to_inventory")

    async def test_el_flete_de_linea_se_suma_entre_las_lineas_del_comprobante(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Es la diferencia con el envío del comprobante, y la razón de que sean
        dos targets: aquél viene repetido y se colapsa; éste es el pedazo de cada
        artículo y se suma. 200 + 150 son 350, no 200."""
        await _importar(
            db_session,
            sample_tenant,
            [
                _linea_de("Vela aromatica", "100", flete_linea="200"),
                _linea_de("Taza ceramica", "100", flete_linea="150"),
            ],
        )

        logistica = [
            g for g in await _gastos(db_session, sample_tenant) if g.category == "LOGISTICS"
        ]
        assert len(logistica) == 1
        assert Decimal(str(logistica[0].amount)) == Decimal("350.00")

    async def test_los_dos_fletes_conviven_sin_taparse(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Cada uno con su ancla de idempotencia: son cargos distintos del mismo
        comprobante y uno no puede colapsar al otro."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(envio="500", flete_linea="300")],
        )

        logistica = sorted(
            Decimal(str(g.amount))
            for g in await _gastos(db_session, sample_tenant)
            if g.category == "LOGISTICS"
        )
        assert logistica == [Decimal("300.00"), Decimal("500.00")]


class TestElEnvioDelComprobanteSeReparteEntreSusLineas:
    """F-H6.d — el reparto por subtotal era un no-op silencioso.

    `build_line_costs` sólo reparte si `shared_shipping > 0`, y el importador
    nunca le pasaba ese argumento: caía al default `Decimal("0")`. Elegir
    «repartir por subtotal» pasaba la validación, devolvía 200 y no movía un
    centavo. La aritmética estaba probada; lo que faltaba era el caller.
    """

    async def test_el_reparto_cuadra_al_centavo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Tres líneas de 100 y un flete de 10: 3,33 + 3,33 + 3,34 = 10 exacto.

        El redondeo por línea no cierra solo (3,33 × 3 = 9,99). El centavo que
        falta va a la línea de mayor base, determinístico. Que la suma dé el
        flete original es la compuerta de la fase: si no cuadra, el costo total
        de la compra no es el que salió de la caja.
        """
        await _importar(
            db_session,
            sample_tenant,
            [
                _linea_de("Vela aromatica", "100", envio="10"),
                _linea_de("Taza ceramica", "100", envio="10"),
                _linea_de("Mantel lino", "100", envio="10"),
            ],
            costos={"shared_shipping": "por_subtotal"},
        )

        costos = await _costos_por_producto(db_session, sample_tenant)
        repartido = sum(costos.values()) - Decimal("300")
        assert repartido == Decimal("10"), f"el reparto no cuadra: {costos}"
        assert sorted(costos.values()) == [
            Decimal("103.33"),
            Decimal("103.33"),
            Decimal("103.34"),
        ]

    async def test_cada_comprobante_reparte_lo_suyo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Una sola llamada sobre la hoja entera le cargaría a una compra el
        flete de la otra. Los totales de la hoja cerrarían igual y el error
        quedaría escondido en el costo por producto."""
        await _importar(
            db_session,
            sample_tenant,
            [
                _linea_de("Vela aromatica", "100", envio="10", comprobante="A-0001"),
                _linea_de("Taza ceramica", "100", envio="60", comprobante="B-0002"),
            ],
            costos={"shared_shipping": "por_subtotal"},
        )

        costos = await _costos_por_producto(db_session, sample_tenant)
        assert costos["Vela aromatica"] == Decimal("110")
        assert costos["Taza ceramica"] == Decimal("160")

    async def test_no_distribuir_es_el_default_y_no_toca_el_costo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control de V6: cambiar el default alteraría el costo de todos los
        imports que ya existen. Sin decisión, el flete no entra al producto."""
        await _importar(
            db_session,
            sample_tenant,
            [_linea_de("Vela aromatica", "100", envio="10")],
        )

        costos = await _costos_por_producto(db_session, sample_tenant)
        assert costos["Vela aromatica"] == Decimal("100")

    async def test_sin_comprobante_no_reparte_aunque_se_pida(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Un 2.000 repetido en diez filas sin comprobante es indistinguible de
        diez envíos de 2.000. No se reparte, igual que no se cobra."""
        await _importar(
            db_session,
            sample_tenant,
            [
                _linea_de("Vela aromatica", "100", envio="10", comprobante=""),
                _linea_de("Taza ceramica", "100", envio="10", comprobante=""),
            ],
            costos={"shared_shipping": "por_subtotal"},
        )

        costos = await _costos_por_producto(db_session, sample_tenant)
        assert costos["Vela aromatica"] == Decimal("100")
        assert costos["Taza ceramica"] == Decimal("100")
