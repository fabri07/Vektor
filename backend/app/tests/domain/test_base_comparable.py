"""M·1d — base comparable del costo según el régimen (puro).

Incluye los ejemplos de aceptación aprobados del plan v3 (fase 2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain.base_comparable import (
    MOTIVO_ENVIO_ADICIONAL,
    MOTIVO_ENVIO_DESCONOCIDO,
    MOTIVO_ESTIMACION,
    MOTIVO_IMPUESTO_DESCONOCIDO,
    MOTIVO_IVA_MAYOR_QUE_COSTO,
    MOTIVO_RECUPERABLE_SIN_CONFIRMAR,
    MOTIVO_RECUPERABLE_SIN_RI,
    MOTIVO_REGIMEN_LEGACY,
    MOTIVO_SIN_COSTO,
    MOTIVO_SIN_IMPORTE_NI_TASA,
    MOTIVO_SIN_REGIMEN,
    BaseComparable,
    base_comparable,
    regimen_para_base,
)
from app.domain.contrato_de_costo import contrato_de_linea_importada
from app.domain.costo_de_referencia import CostoDeReferencia
from app.domain.purchase_cost import BASE_APLICAR, LINEA_AL_COSTO, CostLine, build_line_costs


def _ref(costo: str | None = "100", **over: Any) -> CostoDeReferencia:
    base: dict[str, Any] = {
        "origen": "compra",
        "costo_unitario": Decimal(costo) if costo is not None else None,
        "fecha": datetime(2026, 9, 10, tzinfo=UTC),
        "movement_id": uuid.uuid4(),
        "envio": "incluido",
        "impuesto": "no_aplica",
    }
    base.update(over)
    return CostoDeReferencia(**base)


class TestEjemplosDeAceptacion:
    def test_131_por_unidad_con_envio_e_impuesto_no_recuperable(self) -> None:
        """10 u: mercadería $1.000 + envío $100 + impuesto no recuperable $210."""
        plan = build_line_costs(
            [
                CostLine(
                    row_index=0,
                    amount=Decimal("1000"),
                    quantity=10,
                    taxes=Decimal("210"),
                    shipping_line=Decimal("100"),
                )
            ],
            basis=BASE_APLICAR,
            line_mode=LINEA_AL_COSTO,
        )
        contrato = contrato_de_linea_importada(Decimal("100"), plan.lines[0])
        ref = _ref(
            str(contrato.unit_cost_final),
            envio=contrato.shipping_treatment,
            impuesto=contrato.tax_treatment,
            tax_amount=contrato.tax_amount,
            tax_recoverable=False,
        )
        assert base_comparable(ref, "monotributo") == BaseComparable(Decimal("131.00"), True)
        # Ya venían incluidos: no se vuelven a sumar, ni para un RI que no recupera.
        assert base_comparable(ref, "responsable_inscripto") == BaseComparable(
            Decimal("131.00"), True
        )

    def test_responsable_inscripto_605_con_iva_recuperable_da_500(self) -> None:
        por_importe = _ref(
            "605", impuesto="incluido", tax_amount=Decimal("105"), tax_recoverable=True
        )
        por_tasa = _ref(
            "605",
            envio="no_aplica",
            impuesto="incluido",
            tax_rate=Decimal("0.21"),
            tax_recoverable=True,
        )
        assert base_comparable(por_importe, "responsable_inscripto") == BaseComparable(
            Decimal("500.00"), True
        )
        assert base_comparable(por_tasa, "responsable_inscripto") == BaseComparable(
            Decimal("500.00"), True
        )

    def test_impuesto_no_aplica_es_el_costo_tal_cual(self) -> None:
        assert base_comparable(_ref("100"), "monotributo") == BaseComparable(
            Decimal("100"), True
        )

    def test_tasas_distintas(self) -> None:
        """10,5% (alícuota reducida): $552,50 con IVA recuperable → $500."""
        ref = _ref(
            "552.50",
            envio="no_aplica",
            impuesto="incluido",
            tax_rate=Decimal("0.105"),
            tax_recoverable=True,
        )
        assert base_comparable(ref, "responsable_inscripto").costo == Decimal("500.00")


class TestImpuesto:
    def test_ri_con_iva_adicional_recuperable_ya_es_neto(self) -> None:
        ref = _ref("500", impuesto="adicional", tax_rate=Decimal("0.21"), tax_recoverable=True)
        assert base_comparable(ref, "responsable_inscripto") == BaseComparable(
            Decimal("500"), True
        )

    def test_no_recuperable_adicional_se_suma_una_vez(self) -> None:
        ref = _ref("500", impuesto="adicional", tax_amount=Decimal("105"))
        assert base_comparable(ref, "monotributo") == BaseComparable(Decimal("605.00"), True)

    def test_adicional_sin_importe_ni_tasa_queda_afuera(self) -> None:
        r = base_comparable(_ref("500", impuesto="adicional"), "informal")
        assert (r.confirmada, r.motivo, r.costo) == (
            False,
            MOTIVO_SIN_IMPORTE_NI_TASA,
            Decimal("500"),
        )

    def test_ri_sin_confirmar_si_recupera_queda_afuera(self) -> None:
        r = base_comparable(
            _ref("605", impuesto="incluido", tax_rate=Decimal("0.21")), "responsable_inscripto"
        )
        assert (r.confirmada, r.motivo) == (False, MOTIVO_RECUPERABLE_SIN_CONFIRMAR)

    def test_iva_recuperable_sin_ser_ri_es_inconsistente(self) -> None:
        r = base_comparable(
            _ref("605", impuesto="incluido", tax_recoverable=True), "monotributo"
        )
        assert (r.confirmada, r.motivo) == (False, MOTIVO_RECUPERABLE_SIN_RI)

    def test_impuesto_desconocido_es_estimacion(self) -> None:
        r = base_comparable(_ref("100", impuesto="desconocido"), "monotributo")
        assert (r.costo, r.confirmada, r.motivo) == (
            Decimal("100"),
            False,
            MOTIVO_IMPUESTO_DESCONOCIDO,
        )

    def test_nunca_21_por_defecto(self) -> None:
        """Sin tasa ni importe confirmados no se asume ninguna alícuota."""
        r = base_comparable(
            _ref("605", impuesto="incluido", tax_recoverable=True), "responsable_inscripto"
        )
        assert (r.confirmada, r.motivo) == (False, MOTIVO_SIN_IMPORTE_NI_TASA)


class TestEnvioYOrigen:
    @pytest.mark.parametrize(
        ("envio", "motivo"),
        [("adicional", MOTIVO_ENVIO_ADICIONAL), ("desconocido", MOTIVO_ENVIO_DESCONOCIDO)],
    )
    def test_envio_que_no_esta_en_el_costo_no_es_comparable(
        self, envio: str, motivo: str
    ) -> None:
        r = base_comparable(_ref("100", envio=envio), "monotributo")
        assert (r.confirmada, r.motivo) == (False, motivo)

    def test_envio_no_aplica_es_comparable(self) -> None:
        assert base_comparable(_ref("100", envio="no_aplica"), "monotributo").confirmada

    def test_costo_cargado_es_estimacion(self) -> None:
        ref = CostoDeReferencia(origen="costo_cargado", costo_unitario=Decimal("90"))
        assert base_comparable(ref, "monotributo") == BaseComparable(
            Decimal("90"), False, MOTIVO_ESTIMACION
        )

    def test_sin_costo(self) -> None:
        ref = CostoDeReferencia(origen="sin_costo", costo_unitario=None)
        assert base_comparable(ref, "monotributo") == BaseComparable(
            None, False, MOTIVO_SIN_COSTO
        )


class TestRegimen:
    def test_el_alias_legacy_registered_no_habilita_un_calculo_confirmado(self) -> None:
        assert regimen_para_base("registered") == MOTIVO_REGIMEN_LEGACY
        assert regimen_para_base(" Registered ") == MOTIVO_REGIMEN_LEGACY
        r = base_comparable(_ref("100"), "registered")
        assert (r.confirmada, r.motivo) == (False, MOTIVO_REGIMEN_LEGACY)

    @pytest.mark.parametrize("crudo", [None, "", "algo raro"])
    def test_sin_regimen(self, crudo: str | None) -> None:
        r = base_comparable(_ref("100"), crudo)
        assert (r.confirmada, r.motivo) == (False, MOTIVO_SIN_REGIMEN)

    @pytest.mark.parametrize(
        ("crudo", "esperado"),
        [("RI", "ri"), ("monotributista", "no_recupera"), ("informal", "no_recupera")],
    )
    def test_aliases(self, crudo: str, esperado: str) -> None:
        assert regimen_para_base(crudo) == esperado


class TestRevision:
    """Revisión de M·1d."""

    def test_con_envio_incluido_la_tasa_no_alcanza(self) -> None:
        """P2: $500 + IVA $105 + envío $100 sin IVA = $705. Aplicar el 21% a todo
        daría 582,64 «confirmado»; el neto real es 600. Sin importe, estimación."""
        ref = _ref(
            "705", impuesto="incluido", tax_rate=Decimal("0.21"), tax_recoverable=True
        )
        r = base_comparable(ref, "responsable_inscripto")
        assert (r.confirmada, r.motivo) == (False, MOTIVO_SIN_IMPORTE_NI_TASA)
        con_importe = _ref(
            "705", impuesto="incluido", tax_amount=Decimal("105"), tax_recoverable=True
        )
        assert base_comparable(con_importe, "responsable_inscripto") == BaseComparable(
            Decimal("600.00"), True
        )

    def test_la_tasa_vale_sin_envio(self) -> None:
        ref = _ref(
            "605",
            envio="no_aplica",
            impuesto="incluido",
            tax_rate=Decimal("0.21"),
            tax_recoverable=True,
        )
        assert base_comparable(ref, "responsable_inscripto").costo == Decimal("500.00")

    def test_por_tasa_adicional_no_recuperable(self) -> None:
        """Rama ``adicional`` no recuperable por TASA: sólo vale sin envío."""
        sin_envio = _ref("500", envio="no_aplica", impuesto="adicional", tax_rate=Decimal("0.21"))
        assert base_comparable(sin_envio, "monotributo") == BaseComparable(
            Decimal("605.00"), True
        )
        con_envio = _ref("500", envio="incluido", impuesto="adicional", tax_rate=Decimal("0.21"))
        r = base_comparable(con_envio, "monotributo")
        assert (r.confirmada, r.motivo, r.costo) == (
            False, MOTIVO_SIN_IMPORTE_NI_TASA, Decimal("500")
        )

    def test_iva_mayor_que_el_costo_no_se_confirma(self) -> None:
        ref = _ref("100", impuesto="incluido", tax_amount=Decimal("150"), tax_recoverable=True)
        r = base_comparable(ref, "responsable_inscripto")
        assert (r.confirmada, r.motivo) == (False, MOTIVO_IVA_MAYOR_QUE_COSTO)

    def test_el_stock_inicial_es_estimacion(self) -> None:
        """Valoriza la apertura con el costo del catálogo (decisión 3)."""
        r = base_comparable(_ref("100", origen="stock_inicial"), "monotributo")
        assert (r.confirmada, r.motivo) == (False, MOTIVO_ESTIMACION)
