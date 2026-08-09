"""F-H6.c — aceptación: todo target monetario tiene un consumidor que mueve el costo.

Estas pruebas se escriben ROJAS a propósito, ANTES de implementar F-H6.c.

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
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

#: El agujero, declarado como marca en vez de como comentario.
#:
#: `strict=True` es lo que lo convierte en compuerta: mientras F-H6.c no exista
#: estos tests fallan y el CI sigue verde, pero **en cuanto pasen, pytest los
#: reporta como error** y obliga a sacar la marca. Un `xfail` no estricto sería
#: una nota al pie que nadie vuelve a leer.
_FALTA_FH6C = pytest.mark.xfail(
    strict=True,
    reason=(
        "F-H6.c todavía no existe: el catálogo ofrece `discount`, `taxes` y "
        "`shipping_cost_line` pero el importador no lee sus valores. Sacar esta "
        "marca es parte de implementarlo."
    ),
)

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
        extra["purchase_cost_decisions"] = {_CTX: costos}
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


async def _costo_del_movimiento(db: AsyncSession, tenant: Tenant) -> Decimal:
    """Lo que quedó registrado como costo unitario de ESTA compra.

    Es el observable correcto: `Product.unit_cost_ars` es el costo de referencia
    vigente del producto y lo pisa la última compra (V5), mientras que el
    movimiento dice qué costó esta operación en particular.
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


@pytest.mark.asyncio
class TestElDescuentoYLosImpuestosLleganAlCosto:
    @_FALTA_FH6C
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
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1000.00")

    @_FALTA_FH6C
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
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1250.00")

    @_FALTA_FH6C
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


@pytest.mark.asyncio
class TestLosDosFletesNoSonElMismoCargo:
    @_FALTA_FH6C
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
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1230.00")

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
                _fila(envio="500", articulo="Vela A", total="6000", cantidad="5"),
                _fila(envio="500", articulo="Vela B", total="6000", cantidad="5"),
            ],
        )
        fletes = [
            g
            for g in await _gastos(db_session, sample_tenant)
            if (g.category or "").upper() == "LOGISTICS"
        ]
        assert len(fletes) == 1, "la cifra repetida es un solo cargo"
        assert Decimal(str(fletes[0].amount)) == Decimal("500")

    @_FALTA_FH6C
    async def test_los_dos_fletes_conviven_sin_mezclarse(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Semánticas opuestas en la misma hoja: el del comprobante se cobra una
        vez y queda como gasto de logística; el de línea se capitaliza en el costo.
        Fusionarlos repartiría algo que ya venía repartido."""
        await _importar(
            db_session,
            sample_tenant,
            [_fila(envio="500", flete_linea="300")],
            costos={"line_shipping": "al_costo"},
        )
        assert await _costo_del_movimiento(db_session, sample_tenant) == Decimal("1230.00")
        fletes = [
            g
            for g in await _gastos(db_session, sample_tenant)
            if (g.category or "").upper() == "LOGISTICS"
        ]
        assert len(fletes) == 1
        assert Decimal(str(fletes[0].amount)) == Decimal("500")


@pytest.mark.asyncio
class TestUnValorInvalidoNoEscribeNada:
    @_FALTA_FH6C
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
