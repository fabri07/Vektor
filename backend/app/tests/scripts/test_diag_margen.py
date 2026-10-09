"""Cálculo e informe de ``scripts/diag_margen.py`` (M·0, fase 2).

Las consultas son Postgres-only (``DISTINCT ON``, ``FILTER``, ``?``) y se
verificaron contra ``vektor_pgtest``; acá se prueban las funciones puras: margen
ponderado contra simple, costo 0 como «sin costo» y ausencia de dato ≠ 0.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from diag_margen import (  # noqa: E402
    FUENTE_COGS_EXPENSE,
    FUENTE_LINE_ITEMS,
    FUENTE_SIN_COGS,
    SIN_IMPUESTO,
    DatosTenant,
    LineaVenta,
    Producto,
    Salud,
    clasificar_costo,
    explicar_margen_en_cero,
    informe,
    margen_api,
    margen_dashboard,
    margen_facts,
    margen_ponderado,
    margen_producto,
    promedio_simple,
)

from app.domain.business_time import today_ar

D = Decimal
BASE = "host=localhost base=vektor_pgtest"
HOY = today_ar()
DESDE = HOY - timedelta(days=29)


def _p(precio: str, costo: str | None, categoria: str = "A") -> Producto:
    return Producto(
        nombre=f"p{precio}",
        categoria=categoria,
        precio=D(precio),
        costo=None if costo is None else D(costo),
    )


def test_ponderado_47_contra_simple_35_ejemplo_del_plan() -> None:
    # $100 con costo $80 (20%) y $900 con costo $450 (50%).
    ponderado, cobertura = margen_ponderado([(D("100"), D("80")), (D("900"), D("450"))])
    assert ponderado == D("47")
    assert cobertura == D("1")
    simple = promedio_simple(
        m for p in (_p("100", "80"), _p("900", "450"))
        if (m := margen_producto(p.precio, p.costo)) is not None
    )
    assert simple == D("35")
    # El dashboard de hoy promedia simple: da 35, no 47.
    assert margen_dashboard([_p("100", "80"), _p("900", "450")]) == [("A", D("35"))]


def test_costo_cero_es_sin_costo_no_100() -> None:
    assert clasificar_costo(D("100"), D("0")) == "costo_cero"
    assert margen_producto(D("100"), D("0")) is None
    # Lo que hace HOY la API (ProductResponse.margin_pct): 100%. Se documenta.
    assert margen_api(D("100"), D("0")) == D("100")
    # El ponderado no suma un costo 0 como costo: lo deja fuera de la cobertura.
    margen, cobertura = margen_ponderado([(D("100"), D("0")), (D("100"), D("50"))])
    assert margen == D("50")
    assert cobertura == D("0.5")


def test_clasificacion_de_costos() -> None:
    assert clasificar_costo(D("100"), None) == "costo_nulo"
    assert clasificar_costo(D("0"), D("10")) == "sin_precio"
    assert clasificar_costo(D("100"), D("120")) == "costo_mayor_que_precio"
    assert clasificar_costo(D("100"), D("100")) == "con_costo"


def test_facts_replica_cuenta_costo_cero_como_conocido_y_sus_fallbacks() -> None:
    lineas = [LineaVenta(D("100"), D("0")), LineaVenta(D("100"), None)]
    margen, fuente, conf = margen_facts(lineas, D("0"))
    assert (margen, fuente, conf) == (D("100"), FUENTE_LINE_ITEMS, D("0.5"))
    sin_costo = [LineaVenta(D("200"), None)]
    assert margen_facts(sin_costo, D("50"))[:2] == (D("75"), FUENTE_COGS_EXPENSE)
    assert margen_facts(sin_costo, D("0"))[:2] == (D("100"), FUENTE_SIN_COGS)
    assert margen_facts([], D("0"))[0] is None


def test_sin_medicion_no_es_cero() -> None:
    vacio = DatosTenant(tenant_id="00000000-0000-0000-0000-000000000001")
    texto = "\n".join(informe(BASE, DESDE, HOY, [vacio]))
    assert texto.startswith(f"Base leída: {BASE}")
    assert "sin medición (ningún producto con costo)" in texto
    assert "sin medición (sin ventas en la ventana)" in texto
    assert "sin medición (no hay snapshot)" in texto
    assert SIN_IMPUESTO in texto
    # Ninguna métrica de margen sale como 0% por falta de datos.
    assert "0.0%" not in texto


def test_un_margen_cero_medido_si_sale_como_cero() -> None:
    d = DatosTenant(tenant_id="t", productos=[_p("100", "100")])
    texto = "\n".join(informe(BASE, DESDE, HOY, [d]))
    assert "0.0%" in texto


def test_explica_subscore_cero_por_stock_inicial_como_compra() -> None:
    d = DatosTenant(
        tenant_id="t",
        productos=[_p("100", "60")],
        inventario_bsl=D("7538005"),
        inventario_stock_inicial=D("7538005"),
        salud=Salud(
            fecha=HOY,
            score_margin=0,
            ventas_est=D("1000000"),
            inventario_est=D("7538005"),
            fijos_est=D("0"),
        ),
    )
    lectura = "\n".join(explicar_margen_en_cero(d))
    assert "subscore «Margen» del health score es 0" in lectura
    assert "Stock inicial" in lectura
    assert "No hay ventas REALES en la ventana" in lectura
