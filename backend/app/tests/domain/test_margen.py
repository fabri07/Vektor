"""M·2b — la definición única del margen bruto (puro).

Incluye los ejemplos de aceptación de la fase 2 del plan v3.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.margen import (
    SUPUESTO_ALICUOTA_RI,
    LineaDeMargen,
    medir_margen,
    motivo_cobertura,
)

D = Decimal


def _l(importe: str, costo: str | None = None, confirmado: bool = False) -> LineaDeMargen:
    return LineaDeMargen(D(importe), D(costo) if costo is not None else None, confirmado)


class TestEjemplosDeAceptacion:
    def test_ponderado_por_importe_47_no_35(self) -> None:
        """$100 con costo $80 y $900 con costo $450 → 47%, no el 35% del promedio."""
        m = medir_margen([_l("100", "80", True), _l("900", "450", True)], "monotributo")
        assert m.estado == "confirmado"
        assert m.margen_pct == D(47)
        assert m.ganancia == D(470)

    def test_ri_1210_contra_605_da_50_pero_estimado(self) -> None:
        """Venta $1.210 con IVA, costo neto $500 (605 con IVA recuperable, M·1d)."""
        m = medir_margen([_l("1210", "500", True)], "responsable_inscripto")
        assert m.margen_pct is not None
        assert m.margen_pct.quantize(D("0.01")) == D("50.00")
        # El 21% de la venta es un supuesto: nunca confirma.
        assert (m.estado, m.motivo) == ("estimado", SUPUESTO_ALICUOTA_RI)
        assert m.supuestos == (SUPUESTO_ALICUOTA_RI,)

    def test_monotributo_no_ajusta_el_importe(self) -> None:
        m = medir_margen([_l("1210", "605", True)], "monotributo")
        assert (m.estado, m.margen_pct, m.supuestos) == ("confirmado", D(50), ())

    def test_registrado_legacy_no_se_trata_como_ri(self) -> None:
        m = medir_margen([_l("1000", "500", True)], "registered")
        assert m.margen_pct == D(50)
        assert m.supuestos == ()


class TestCobertura:
    def test_venta_sin_producto_entra_en_el_denominador(self) -> None:
        """La cobertura es sobre TODO lo vendido: 40% con costo no alcanza."""
        m = medir_margen([_l("400", "200", True), _l("600")], None)
        assert m.estado == "ausente"
        assert m.cobertura_total == D("0.4")
        assert m.margen_pct is None and m.costo_vendido is None and m.ganancia is None
        assert m.motivo == motivo_cobertura(D("0.4")) == "cobertura de costo 40,0%"

    def test_ratio_del_cubierto_proyecta_el_costo_de_lo_no_cubierto(self) -> None:
        """60% cubierto al 50%: el costo vendido es 50% de TODO, no costo 0 al resto."""
        m = medir_margen([_l("600", "300", True), _l("400")], None)
        assert m.estado == "confirmado"
        assert m.margen_pct == D(50)
        assert m.costo_vendido == D(500)
        assert m.ganancia == D(500)

    @pytest.mark.parametrize(
        ("cubierto", "esperado"),
        [("4999", "ausente"), ("5000", "confirmado"), ("5001", "confirmado")],
    )
    def test_bordes_del_50(self, cubierto: str, esperado: str) -> None:
        resto = str(10000 - int(cubierto))
        m = medir_margen([_l(cubierto, "1", True), _l(resto)], None)
        assert m.estado == esperado

    def test_confirmado_bajo_50_con_estimaciones_es_estimado(self) -> None:
        m = medir_margen(
            [_l("300", "150", True), _l("500", "400"), _l("200")], "monotributo"
        )
        assert m.estado == "estimado"
        assert m.cobertura_confirmada == D("0.3")
        assert m.cobertura_total == D("0.8")
        # Ratio de confirmadas + estimadas: 550 / 800.
        assert m.margen_pct == (1 - D(550) / D(800)) * 100
        assert m.motivo is not None and "30,0%" in m.motivo
        assert m.cobertura_del_estado == D("0.8")

    def test_ni_con_estimaciones_llega_es_ausente(self) -> None:
        m = medir_margen([_l("300", "150", True), _l("100", "50"), _l("600")], None)
        assert m.estado == "ausente"
        assert m.motivo == "cobertura de costo 40,0%"


class TestBordes:
    def test_margen_negativo_vale(self) -> None:
        m = medir_margen([_l("100", "130", True)], None)
        assert (m.estado, m.margen_pct) == ("confirmado", D(-30))

    def test_costo_cero_confirmado_cuenta_como_cubierto(self) -> None:
        m = medir_margen([_l("100", "0", True)], None)
        assert (m.estado, m.margen_pct) == ("confirmado", D(100))

    def test_sin_ventas_es_ausente(self) -> None:
        m = medir_margen([], None)
        assert (m.estado, m.margen_pct, m.motivo) == ("ausente", None, "sin ventas en el período")

    def test_confirmado_sin_costo_no_cuenta(self) -> None:
        """Un ``confirmado`` sin costo (no debería existir) no suma cobertura."""
        m = medir_margen([LineaDeMargen(D(100), None, True)], None)
        assert m.estado == "ausente"
