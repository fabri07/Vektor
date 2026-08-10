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
            purchase_cost_decisions={
                "": PurchaseCostDecision(context_id="", base="monto_sin_ajustes")
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
            purchase_cost_decisions={
                "": PurchaseCostDecision(context_id="", line_shipping="al_costo")
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
