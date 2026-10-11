"""M·1b — el contrato del costo que escribe cada ruta de compra (puro).

Lo que se fija acá: lo que la ruta no sabe queda ``desconocido``, nunca un
default que parezca dato. En particular, ``monto_incluye`` no prueba IVA.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.contrato_de_costo import (
    MAX_DOCUMENT_REF,
    ContratoDeCosto,
    contrato_de_linea_importada,
    contrato_declarado,
)
from app.domain.costo_de_referencia import CONTRATO_VERSION
from app.domain.purchase_cost import (
    BASE_APLICAR,
    BASE_INCLUYE,
    COMPARTIDO_SUBTOTAL,
    LINEA_AL_COSTO,
    CostLine,
    build_line_costs,
)


def _linea(**over: object) -> CostLine:
    base: dict[str, object] = {"row_index": 0, "amount": Decimal("1000"), "quantity": 10}
    base.update(over)
    return CostLine(**base)  # type: ignore[arg-type]


class TestElMotorDiceLoQueElContratoNecesita:
    def test_impuesto_sumado_por_unidad_solo_con_monto_sin_ajustes(self) -> None:
        aplicar = build_line_costs([_linea(taxes=Decimal("210"))], basis=BASE_APLICAR)
        incluye = build_line_costs([_linea(taxes=Decimal("210"))], basis=BASE_INCLUYE)
        assert aplicar.lines[0].impuesto_unitario == Decimal("21.00")
        assert incluye.lines[0].impuesto_unitario is None

    def test_sin_cantidad_no_hay_impuesto_por_unidad(self) -> None:
        plan = build_line_costs([_linea(taxes=Decimal("210"), quantity=0)], basis=BASE_APLICAR)
        assert plan.lines[0].impuesto_unitario is None

    def test_envio_de_linea_como_gasto_queda_fuera_del_costo(self) -> None:
        plan = build_line_costs([_linea(shipping_line=Decimal("100"))])
        assert plan.lines[0].envio_fuera_del_costo is True
        al_costo = build_line_costs(
            [_linea(shipping_line=Decimal("100"))], line_mode=LINEA_AL_COSTO
        )
        assert al_costo.lines[0].envio_fuera_del_costo is False

    def test_envio_compartido_sin_repartir_queda_fuera_y_repartido_no(self) -> None:
        sin = build_line_costs([_linea()], shared_shipping=Decimal("100"))
        con = build_line_costs(
            [_linea()], shared_shipping=Decimal("100"), shared_mode=COMPARTIDO_SUBTOTAL
        )
        assert sin.lines[0].envio_fuera_del_costo is True
        assert con.lines[0].envio_fuera_del_costo is False

    def test_sin_envio_no_hay_nada_fuera(self) -> None:
        assert build_line_costs([_linea()]).lines[0].envio_fuera_del_costo is False


class TestContratoDeLineaImportada:
    def test_sin_plan_todo_desconocido_y_el_costo_es_el_facturado(self) -> None:
        c = contrato_de_linea_importada(Decimal("120"), None, document_ref=" A-1 ")
        assert c == ContratoDeCosto(unit_cost_final=Decimal("120"), document_ref="A-1")
        assert c.version == CONTRATO_VERSION

    def test_aceptacion_131_por_unidad(self) -> None:
        """Ejemplo aprobado del plan: $1.000 + envío $100 + impuesto $210, 10 u."""
        plan = build_line_costs(
            [_linea(taxes=Decimal("210"), shipping_line=Decimal("100"))],
            basis=BASE_APLICAR,
            line_mode=LINEA_AL_COSTO,
        )
        c = contrato_de_linea_importada(Decimal("100"), plan.lines[0])
        assert c.unit_cost_final == Decimal("131.00")
        assert c.shipping_treatment == "incluido"
        assert c.tax_treatment == "incluido"
        assert c.tax_amount == Decimal("21.00")
        # Nunca se confirma por defecto ni la tasa ni si es recuperable.
        assert c.tax_rate is None
        assert c.tax_recoverable is None

    def test_monto_incluye_no_prueba_iva(self) -> None:
        plan = build_line_costs([_linea(taxes=Decimal("210"))], basis=BASE_INCLUYE)
        c = contrato_de_linea_importada(Decimal("100"), plan.lines[0])
        assert c.tax_treatment == "desconocido"
        assert c.tax_amount is None

    def test_envio_aparte_es_adicional(self) -> None:
        plan = build_line_costs([_linea(shipping_line=Decimal("100"))])
        c = contrato_de_linea_importada(Decimal("100"), plan.lines[0])
        assert c.shipping_treatment == "adicional"
        assert c.unit_cost_final == Decimal("100.00")

    def test_hoja_con_ajustes_pero_sin_envio_deja_el_envio_desconocido(self) -> None:
        """Una columna de descuento mapeada no dice nada sobre el envío."""
        plan = build_line_costs([_linea(discount=Decimal("100"))], basis=BASE_APLICAR)
        c = contrato_de_linea_importada(Decimal("100"), plan.lines[0])
        assert c.shipping_treatment == "desconocido"
        assert c.unit_cost_final == Decimal("90.00")

    def test_sin_unitario_del_motor_cae_al_facturado(self) -> None:
        plan = build_line_costs([_linea(quantity=0)])
        c = contrato_de_linea_importada(Decimal("100"), plan.lines[0])
        assert c.unit_cost_final == Decimal("100")


class TestContratoDeclarado:
    def test_lo_no_declarado_queda_desconocido(self) -> None:
        c = contrato_declarado(Decimal("50"))
        assert (c.shipping_treatment, c.tax_treatment) == ("desconocido", "desconocido")
        assert c.columnas()["cost_contract_version"] == CONTRATO_VERSION

    def test_comprobante_se_recorta_al_largo_de_la_columna(self) -> None:
        c = contrato_declarado(Decimal("50"), document_ref="x" * 300)
        assert c.document_ref is not None and len(c.document_ref) == MAX_DOCUMENT_REF

    def test_comprobante_en_blanco_es_none(self) -> None:
        assert contrato_declarado(Decimal("50"), document_ref="   ").document_ref is None

    @pytest.mark.parametrize(
        "malo",
        [
            {"shipping_treatment": "gratis"},
            {"tax_treatment": "sí"},
            {"unit_cost_final": Decimal("-1")},
            {"tax_amount": Decimal("-1")},
        ],
    )
    def test_valores_invalidos_se_rechazan_antes_de_la_base(self, malo: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            ContratoDeCosto(**{"unit_cost_final": Decimal("1"), **malo})  # type: ignore[arg-type]
