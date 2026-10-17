"""Cálculo e informe de ``scripts/diag_margen.py`` (M·0, fase 2).

Las consultas son Postgres-only (``DISTINCT ON``, ``FILTER``, ``?``) y se
verificaron contra ``vektor_pgtest``; acá se prueban las funciones puras: margen
ponderado contra simple, costo 0 como «sin costo» y ausencia de dato ≠ 0.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from diag_margen import (  # noqa: E402
    SIN_IMPUESTO,
    DatosTenant,
    LineaVenta,
    Producto,
    Salud,
    clasificar_costo,
    explicar_margen_en_cero,
    informe,
    informe_tenant,
    margen_api,
    margen_dashboard,
    margen_facts,
    margen_neto_mes_cerrado,
    margen_ponderado,
    margen_producto,
    promedio_simple,
    subscore_con_neto,
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
    # La API (ProductResponse.margin_pct) daba 100%; desde M·3-2, sin número.
    assert margen_api(D("100"), D("0")) is None
    # El ponderado no suma un costo 0 como costo: lo deja fuera de la cobertura.
    margen, cobertura = margen_ponderado([(D("100"), D("0")), (D("100"), D("50"))])
    assert margen == D("50")
    assert cobertura == D("0.5")


def test_clasificacion_de_costos() -> None:
    assert clasificar_costo(D("100"), None) == "costo_nulo"
    assert clasificar_costo(D("0"), D("10")) == "sin_precio"
    assert clasificar_costo(D("100"), D("120")) == "costo_mayor_que_precio"
    assert clasificar_costo(D("100"), D("100")) == "con_costo"


def test_facts_usa_la_definicion_unica() -> None:
    """M·2b: el snapshot de la venta, cobertura sobre todo lo vendido, sin fallbacks."""
    lineas = [LineaVenta(D("100"), D("50"), True), LineaVenta(D("100"), None)]
    m = margen_facts(lineas, "monotributo")
    assert (m.estado, m.margen_pct, m.cobertura_total) == ("confirmado", D("50"), D("0.5"))
    sin_costo = [LineaVenta(D("200"), None)]
    assert margen_facts(sin_costo, None).estado == "ausente"
    assert margen_facts([], None).margen_pct is None


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


def test_margen_salud_de_un_snapshot_nuevo_usa_el_costo_vendido() -> None:
    """M·2b-3: el snapshot nuevo trae ingreso y costo VENDIDO de FactsService; la
    compra de mercadería del mes ya no entra."""
    from datetime import date  # noqa: PLC0415

    from diag_margen import Salud, margen_salud  # noqa: PLC0415

    s = Salud(
        fecha=date(2026, 10, 12), score_margin=80, ventas_est=Decimal("100000"),
        inventario_est=Decimal("876000"), fijos_est=Decimal("10000"),
        margen_estado="estimado", ingreso_margen=Decimal("100000"),
        costo_vendido=Decimal("60000"),
    )
    assert margen_salud(s) == Decimal("30")
    ausente = Salud(
        fecha=date(2026, 10, 12), score_margin=None, ventas_est=Decimal("100000"),
        inventario_est=Decimal("876000"), fijos_est=Decimal("10000"),
        margen_estado="ausente", margen_motivo="cobertura de costo 9,0%",
    )
    assert margen_salud(ausente) is None


def test_sombra_m31_neto_del_mes_cerrado_y_subscore() -> None:
    d = DatosTenant(
        tenant_id="t", rubro="kiosco_almacen",
        lineas_mes_cerrado=[LineaVenta(Decimal("10000"), Decimal("6000"), True)],
        gastos_operativos_mes=Decimal("1500"), gastos_operativos_mes_n=2,
    )
    neto = margen_neto_mes_cerrado(d)
    assert neto.margen_pct == Decimal("25")
    assert subscore_con_neto(d, neto) is not None


def test_sombra_m33_el_iva_de_un_ri_no_resta() -> None:
    """La sombra espeja ``FactsService._iva_de_ri`` (revisión de M·3-3)."""
    base: dict[str, Any] = {
        "tenant_id": "t", "rubro": "kiosco_almacen",
        "lineas_mes_cerrado": [LineaVenta(Decimal("12100"), Decimal("6000"), True)],
        "gastos_operativos_mes": Decimal("1500"), "gastos_operativos_mes_n": 2,
        "iva_mes": Decimal("500"), "iva_mes_n": 1,
    }
    ri = margen_neto_mes_cerrado(DatosTenant(**base, regimen="responsable_inscripto"))
    mono = margen_neto_mes_cerrado(DatosTenant(**base, regimen="monotributo"))
    assert ri.gastos_operativos == Decimal("1000")
    assert mono.gastos_operativos == Decimal("1500")
    # Sólo IVA en el mes de un RI: no hay gastos que restar → sin medir.
    solo_iva = DatosTenant(**{**base, "gastos_operativos_mes": Decimal("500"),
                              "gastos_operativos_mes_n": 1}, regimen="responsable_inscripto")
    assert margen_neto_mes_cerrado(solo_iva).margen_pct is None


def test_sombra_m31_sin_gastos_no_inventa() -> None:
    d = DatosTenant(
        tenant_id="t", rubro="kiosco_almacen",
        lineas_mes_cerrado=[LineaVenta(Decimal("10000"), Decimal("6000"), True)],
    )
    neto = margen_neto_mes_cerrado(d)
    assert neto.margen_pct is None
    assert neto.motivo == "sin gastos cargados en el mes"
    assert subscore_con_neto(d, neto) is None
    texto = "\n".join(informe_tenant(d, date(2026, 9, 17), date(2026, 10, 16)))
    assert "M·3-1 sombra: margen neto del último mes cerrado" in texto
    assert "sin medición (no entra en el total)" in texto
