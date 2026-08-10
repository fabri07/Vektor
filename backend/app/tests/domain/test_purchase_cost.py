"""F-H6.c — el costo de una línea de compra, con flete, descuento e impuestos.

Lo que se prueba acá es aritmética con plata: que el reparto sume EXACTAMENTE lo
que había para repartir, que dos corridas den lo mismo, y que ningún camino
invente un costo que el archivo no declaró.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.purchase_cost import (
    BASE_APLICAR,
    BASE_INCLUYE,
    COMPARTIDO_NO,
    COMPARTIDO_SUBTOTAL,
    CON_FLETE,
    LINEA_AL_COSTO,
    LINEA_GASTO,
    MOTIVO_SIN_BASE,
    SIN_FLETE,
    CostLine,
    build_line_costs,
    debe_pisar_costo_de_referencia,
)


def _linea(
    row_index: int,
    amount: str,
    *,
    quantity: int = 0,
    discount: str = "0",
    taxes: str = "0",
    shipping_line: str = "0",
) -> CostLine:
    return CostLine(
        row_index=row_index,
        amount=Decimal(amount),
        quantity=quantity,
        discount=Decimal(discount),
        taxes=Decimal(taxes),
        shipping_line=Decimal(shipping_line),
    )


class TestElRepartoCuadraAlCentavo:
    def test_tres_lineas_iguales_sobre_diez_pesos_suman_diez(self) -> None:
        # 10/3 = 3,3333… → 3,33 tres veces son 9,99. El centavo que falta no se
        # puede tirar: la suma de lo repartido ES el flete, o el costo total de la
        # compra deja de coincidir con lo que se pagó.
        plan = build_line_costs(
            [_linea(i, "10") for i in range(3)],
            shared_shipping=Decimal("10"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        repartos = [line.shipping_allocated for line in plan.lines]
        assert sum(repartos) == Decimal("10")
        assert plan.repartido == Decimal("10")
        assert sorted(repartos) == [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")]

    def test_el_resto_va_siempre_a_la_misma_linea(self) -> None:
        # Determinismo: con bases iguales el desempate es por índice, así que dos
        # corridas sobre el mismo archivo dan el mismo reparto fila por fila.
        primera = build_line_costs(
            [_linea(i, "10") for i in range(3)],
            shared_shipping=Decimal("10"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )
        segunda = build_line_costs(
            [_linea(i, "10") for i in range(3)],
            shared_shipping=Decimal("10"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        assert primera.lines[0].shipping_allocated == Decimal("3.34")
        assert [line.shipping_allocated for line in primera.lines] == [
            line.shipping_allocated for line in segunda.lines
        ]

    def test_el_resto_va_a_la_linea_de_mayor_base(self) -> None:
        plan = build_line_costs(
            [_linea(0, "1"), _linea(1, "1"), _linea(2, "1000")],
            shared_shipping=Decimal("10"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        assert sum(line.shipping_allocated for line in plan.lines) == Decimal("10")
        # La de mayor base absorbe el ajuste: sobre $1000 un centavo no mueve el
        # costo unitario; sobre $1 lo movería un 1%.
        mayor = plan.lines[2]
        assert mayor.shipping_allocated > plan.lines[0].shipping_allocated

    def test_reparte_en_proporcion_al_peso_de_cada_linea(self) -> None:
        plan = build_line_costs(
            [_linea(0, "100"), _linea(1, "300")],
            shared_shipping=Decimal("40"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        assert plan.lines[0].shipping_allocated == Decimal("10")
        assert plan.lines[1].shipping_allocated == Decimal("30")

    def test_el_sobrante_negativo_tambien_se_corrige(self) -> None:
        # Dos líneas iguales sobre un centavo: cada mitad redondea PARA ARRIBA y
        # entre las dos reparten 2 centavos de un flete de 1. El ajuste acá resta.
        plan = build_line_costs(
            [_linea(0, "10"), _linea(1, "10")],
            shared_shipping=Decimal("0.01"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        assert sum(line.shipping_allocated for line in plan.lines) == Decimal("0.01")


class TestNoDistribuirEsLaDecisionPorDefecto:
    def test_sin_pedirlo_no_reparte_nada(self) -> None:
        plan = build_line_costs(
            [_linea(0, "100"), _linea(1, "300")],
            shared_shipping=Decimal("40"),
        )

        assert all(line.shipping_allocated == Decimal("0") for line in plan.lines)
        assert plan.repartido == Decimal("0")
        assert plan.sin_repartir == Decimal("40")
        # No es un fallo: es la decisión. Un motivo acá haría que el import
        # reporte como problema lo que el usuario eligió.
        assert plan.motivo_sin_repartir is None

    def test_sin_flete_compartido_el_costo_es_la_base(self) -> None:
        plan = build_line_costs([_linea(0, "100")], shared_mode=COMPARTIDO_SUBTOTAL)

        assert plan.lines[0].total == Decimal("100")
        assert plan.motivo_sin_repartir is None

    def test_los_defaults_son_los_que_no_tocan_nada(self) -> None:
        # Clava CUÁLES son los defaults, no sólo que existen: los tres ejes
        # arrancan en la opción que no modifica ningún costo. Que el default sea
        # el inofensivo es una regla del programa, no una casualidad de firma.
        lineas = [_linea(0, "100", discount="10", taxes="21", shipping_line="20")]
        implicito = build_line_costs(lineas, shared_shipping=Decimal("40"))
        explicito = build_line_costs(
            lineas,
            shared_shipping=Decimal("40"),
            shared_mode=COMPARTIDO_NO,
            line_mode=LINEA_GASTO,
            basis=BASE_INCLUYE,
        )

        assert implicito.lines[0].total == explicito.lines[0].total == Decimal("100")


class TestSinBaseNoSeReparte:
    def test_todas_las_lineas_en_cero_declara_el_motivo(self) -> None:
        # Repartir en partes iguales sería elegir un criterio que nadie pidió.
        plan = build_line_costs(
            [_linea(0, "0"), _linea(1, "0")],
            shared_shipping=Decimal("50"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        assert plan.repartido == Decimal("0")
        assert plan.sin_repartir == Decimal("50")
        assert plan.motivo_sin_repartir == MOTIVO_SIN_BASE

    def test_sin_lineas_no_explota(self) -> None:
        plan = build_line_costs(
            [], shared_shipping=Decimal("50"), shared_mode=COMPARTIDO_SUBTOTAL
        )

        assert plan.lines == []
        assert plan.sin_repartir == Decimal("50")
        assert plan.motivo_sin_repartir == MOTIVO_SIN_BASE


class TestLosDosFletesSonEjesDistintos:
    def test_el_flete_de_linea_no_entra_al_costo_por_default(self) -> None:
        plan = build_line_costs([_linea(0, "100", shipping_line="20")])

        assert plan.lines[0].total == Decimal("100")

    def test_capitalizado_entra_sin_repartirse(self) -> None:
        # Ya viene asignado por el archivo: repartirlo otra vez sería repartir dos
        # veces el mismo peso.
        plan = build_line_costs(
            [_linea(0, "100", shipping_line="20"), _linea(1, "300", shipping_line="5")],
            line_mode=LINEA_AL_COSTO,
        )

        assert plan.lines[0].total == Decimal("120")
        assert plan.lines[1].total == Decimal("305")

    def test_los_dos_fletes_conviven_en_la_misma_linea(self) -> None:
        plan = build_line_costs(
            [_linea(0, "100", shipping_line="20"), _linea(1, "300", shipping_line="5")],
            shared_shipping=Decimal("40"),
            shared_mode=COMPARTIDO_SUBTOTAL,
            line_mode=LINEA_AL_COSTO,
        )

        assert plan.lines[0].total == Decimal("130")  # 100 + 20 propio + 10 repartido
        assert plan.lines[1].total == Decimal("335")  # 300 + 5 propio + 30 repartido

    def test_capitalizar_el_de_linea_no_activa_el_reparto_del_compartido(self) -> None:
        # Control del eje: si `al_costo` arrastrara el reparto, la decisión de un
        # eje estaría decidiendo el otro.
        plan = build_line_costs(
            [_linea(0, "100", shipping_line="20")],
            shared_shipping=Decimal("40"),
            line_mode=LINEA_AL_COSTO,
        )

        assert plan.lines[0].shipping_allocated == Decimal("0")
        assert plan.lines[0].total == Decimal("120")


class TestLaBaseLaDeclaraElUsuario:
    def test_por_default_el_monto_ya_los_incluye(self) -> None:
        # El que no cambia números: un total que ya viene neto no se vuelve a netear.
        plan = build_line_costs([_linea(0, "100", discount="10", taxes="21")])

        assert plan.lines[0].base == Decimal("100")
        assert plan.lines[0].total == Decimal("100")

    def test_declarado_bruto_se_le_aplican(self) -> None:
        plan = build_line_costs(
            [_linea(0, "100", discount="10", taxes="21")], basis=BASE_APLICAR
        )

        assert plan.lines[0].base == Decimal("111")

    def test_la_base_declarada_es_la_que_pondera_el_reparto(self) -> None:
        # Sin esto el flete se repartiría por el bruto mientras el costo usa el
        # neto: dos números distintos para el mismo peso de la línea.
        plan = build_line_costs(
            [_linea(0, "100", discount="50"), _linea(1, "100")],
            shared_shipping=Decimal("30"),
            shared_mode=COMPARTIDO_SUBTOTAL,
            basis=BASE_APLICAR,
        )

        assert plan.lines[0].shipping_allocated == Decimal("10")
        assert plan.lines[1].shipping_allocated == Decimal("20")

    def test_un_descuento_mayor_que_el_monto_no_da_costo_negativo(self) -> None:
        plan = build_line_costs(
            [_linea(0, "100", discount="150")], basis=BASE_APLICAR
        )

        assert plan.lines[0].base == Decimal("0")
        assert plan.lines[0].total == Decimal("0")
        # Se declara: un costo en cero es un dato raro y el dueño lo tiene que ver.
        assert plan.lines[0].descuento_mayor_que_monto is True

    def test_la_linea_sana_no_se_marca(self) -> None:
        plan = build_line_costs([_linea(0, "100", discount="10")], basis=BASE_APLICAR)

        assert plan.lines[0].descuento_mayor_que_monto is False


class TestCostoUnitarioFinal:
    def test_divide_el_total_por_la_cantidad_recibida(self) -> None:
        plan = build_line_costs(
            [_linea(0, "100", quantity=4)],
            shared_shipping=Decimal("20"),
            shared_mode=COMPARTIDO_SUBTOTAL,
        )

        assert plan.lines[0].total == Decimal("120")
        assert plan.lines[0].unit_cost_final == Decimal("30")

    def test_sin_cantidad_no_se_inventa_un_divisor(self) -> None:
        plan = build_line_costs([_linea(0, "100")])

        assert plan.lines[0].unit_cost_final is None

    def test_redondea_al_centavo(self) -> None:
        plan = build_line_costs([_linea(0, "10", quantity=3)])

        assert plan.lines[0].unit_cost_final == Decimal("3.33")


class TestModosDesconocidos:
    def test_un_modo_compartido_invalido_no_se_comporta_como_el_default(self) -> None:
        # El schema del confirm restringe con `Literal`, pero el dominio no puede
        # confiar en su único caller: caer al default silencioso fue el agujero que
        # ya tenía `plan_shipping_charges` con `sin_comprobante`.
        with pytest.raises(ValueError, match="shared_mode"):
            build_line_costs([_linea(0, "100")], shared_mode="lo_que_sea")

    def test_un_modo_de_linea_invalido_tampoco(self) -> None:
        with pytest.raises(ValueError, match="line_mode"):
            build_line_costs([_linea(0, "100")], line_mode="lo_que_sea")

    def test_una_base_invalida_tampoco(self) -> None:
        with pytest.raises(ValueError, match="basis"):
            build_line_costs([_linea(0, "100")], basis="lo_que_sea")


class TestElIndicePorFila:
    def test_by_row_es_lo_que_consume_el_importador(self) -> None:
        plan = build_line_costs([_linea(7, "100"), _linea(3, "50")])

        indice = plan.by_row()
        assert set(indice) == {7, 3}
        assert indice[7].base == Decimal("100")


class TestUnaCompraNuevaNoPisaCualquierCosto:
    """V5 — el caso peor: 110 con flete implícito pisado por 100 facturado.

    El mismo producto entra una vez sin desglose (el proveedor cargó el flete en
    el precio) y después desglosado. El costo "baja" y nada se abarató: cambió el
    formato de la planilla. El margen que se calcula contra ese costo pasa a
    mentir, y no hay ninguna señal de que algo se rompió.
    """

    @pytest.mark.parametrize(
        ("entrante", "guardado", "costo_guardado", "pisa"),
        [
            # El costo final CON flete es lo que el negocio pagó: manda siempre.
            pytest.param(True, CON_FLETE, "110", True, id="con_flete_sobre_con_flete"),
            pytest.param(True, SIN_FLETE, "100", True, id="con_flete_sobre_sin_flete"),
            # El caso de V5: bajaría de 110 a 100 sin que nada se abarate.
            pytest.param(False, CON_FLETE, "110", False, id="facturado_no_pisa_con_flete"),
            pytest.param(None, CON_FLETE, "110", False, id="desconocido_tampoco_pisa"),
            # Sin nada que preservar, entra.
            pytest.param(False, SIN_FLETE, "100", True, id="facturado_sobre_sin_flete"),
            pytest.param(False, None, "100", True, id="facturado_sobre_desconocido"),
            # Control obligatorio: sin estas dos filas el guard apagaría la carga
            # inicial de costos y el stock quedaría valuado en cero.
            pytest.param(False, CON_FLETE, None, True, id="producto_sin_costo"),
            pytest.param(False, CON_FLETE, "0", True, id="costo_cero_es_sin_costo"),
        ],
    )
    def test_tabla(
        self,
        entrante: bool | None,
        guardado: str | None,
        costo_guardado: str | None,
        pisa: bool,
    ) -> None:
        assert (
            debe_pisar_costo_de_referencia(
                entrante_incluye_flete=entrante,
                guardado_incluye_flete=guardado,
                costo_guardado=(
                    Decimal(costo_guardado) if costo_guardado is not None else None
                ),
            )
            is pisa
        )
