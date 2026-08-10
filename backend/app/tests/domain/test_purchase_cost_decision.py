"""F-H6.c — una decisión sobre el costo que no se puede honrar no se ignora.

Lo que estos tests fijan es la diferencia entre las dos salidas, que es de
producto y no de implementación:

- **Error (422, antes del lease):** el usuario declaró un efecto que no va a
  ocurrir. Se corta, porque cree haber resuelto algo sobre sus costos.
- **Aviso (el import sigue):** el usuario NO declaró nada y mapeó una columna de
  costo. El default no cambia números que nadie pidió cambiar, pero tampoco puede
  callarse: mapear un descuento y que no pase nada, sin decirlo, es el no-op
  silencioso que esta fase vino a eliminar.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.purchase_cost import (
    BASE_APLICAR,
    BASE_INCLUYE,
    COMPARTIDO_NO,
    COMPARTIDO_SUBTOTAL,
    LINEA_AL_COSTO,
    LINEA_GASTO,
)
from app.domain.purchase_cost_decision import (
    AJUSTE_ILEGIBLE,
    PurchaseCostDecision,
    hojas_que_necesitan_aviso,
    parse_ajuste,
    texto_del_ajuste_ilegible,
    texto_del_aviso,
    validate_purchase_cost_decisions,
)

_CTX = "sheet:Compras"

_MAPEO_COMPLETO = {
    "fecha": "expense_date",
    "total": "amount",
    "cantidad": "quantity",
    "descuento": "discount",
    "iva": "taxes",
    "flete_linea": "shipping_cost_line",
    "envio": "shipping_cost",
}

_MAPEO_PELADO = {"fecha": "expense_date", "total": "amount"}


def _motivos(errores: list[tuple[str, str]]) -> list[str]:
    return [m for m, _texto in errores]


class TestLosDefaultsNoTocanNada:
    def test_una_decision_por_default_es_honrable_sobre_cualquier_hoja(self) -> None:
        """Los tres ejes arrancan en «no toques nada», igual que el remito manual."""
        dec = PurchaseCostDecision(context_id=_CTX)
        assert dec.base == BASE_INCLUYE
        assert dec.shared_shipping == COMPARTIDO_NO
        assert dec.line_shipping == LINEA_GASTO
        assert validate_purchase_cost_decisions([dec], {_CTX: _MAPEO_PELADO}) == []

    def test_sin_decisiones_no_hay_nada_que_validar(self) -> None:
        assert validate_purchase_cost_decisions([], {_CTX: _MAPEO_COMPLETO}) == []


class TestUnModoQueNoExisteCortaAntes:
    def test_una_base_desconocida_se_rechaza(self) -> None:
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id=_CTX, base="modo_que_no_existe")],
            {_CTX: _MAPEO_COMPLETO},
        )
        assert _motivos(errores) == ["base_de_costo_desconocida"]

    def test_y_los_dos_modos_de_envio_tambien(self) -> None:
        errores = validate_purchase_cost_decisions(
            [
                PurchaseCostDecision(
                    context_id=_CTX, shared_shipping="???", line_shipping="???"
                )
            ],
            {_CTX: _MAPEO_COMPLETO},
        )
        assert set(_motivos(errores)) == {
            "modo_de_envio_compartido_desconocido",
            "modo_de_envio_de_linea_desconocido",
        }

    def test_un_modo_invalido_no_arrastra_mensajes_sobre_columnas(self) -> None:
        """Con una decisión que no se entiende, hablar de qué columna le falta es
        ruido sobre un error que el usuario ya tiene que corregir."""
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id=_CTX, base="???")],
            {_CTX: _MAPEO_PELADO},
        )
        assert _motivos(errores) == ["base_de_costo_desconocida"]


class TestDeclararUnEfectoQueNoVaAOcurrir:
    def test_aplicar_ajustes_sin_columna_de_descuento_ni_impuesto(self) -> None:
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id=_CTX, base=BASE_APLICAR)],
            {_CTX: _MAPEO_PELADO},
        )
        assert _motivos(errores) == ["base_sin_columnas_de_ajuste"]

    def test_pero_alcanza_con_una_de_las_dos(self) -> None:
        """Una planilla real puede traer descuento y no IVA."""
        solo_descuento = {**_MAPEO_PELADO, "descuento": "discount"}
        assert (
            validate_purchase_cost_decisions(
                [PurchaseCostDecision(context_id=_CTX, base=BASE_APLICAR)],
                {_CTX: solo_descuento},
            )
            == []
        )

    def test_capitalizar_el_flete_de_linea_sin_esa_columna(self) -> None:
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id=_CTX, line_shipping=LINEA_AL_COSTO)],
            {_CTX: _MAPEO_PELADO},
        )
        assert _motivos(errores) == ["flete_de_linea_sin_columna"]

    def test_repartir_el_flete_compartido_sin_esa_columna(self) -> None:
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id=_CTX, shared_shipping=COMPARTIDO_SUBTOTAL)],
            {_CTX: _MAPEO_PELADO},
        )
        assert _motivos(errores) == ["flete_compartido_sin_columna"]

    def test_los_dos_fletes_se_exigen_por_separado(self) -> None:
        """Semánticas opuestas: capitalizar el de línea no habilita repartir el
        del comprobante, ni al revés."""
        solo_linea = {**_MAPEO_PELADO, "flete_linea": "shipping_cost_line"}
        errores = validate_purchase_cost_decisions(
            [
                PurchaseCostDecision(
                    context_id=_CTX,
                    line_shipping=LINEA_AL_COSTO,
                    shared_shipping=COMPARTIDO_SUBTOTAL,
                )
            ],
            {_CTX: solo_linea},
        )
        assert _motivos(errores) == ["flete_compartido_sin_columna"]

    def test_una_decision_sobre_una_hoja_que_no_existe(self) -> None:
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id="sheet:Fantasma", base=BASE_APLICAR)],
            {_CTX: _MAPEO_COMPLETO},
        )
        assert _motivos(errores) == ["decision_de_costo_sin_hoja"]


class TestElMensajeLoLeeUnaPersona:
    def test_nombra_la_hoja_y_el_campo_en_castellano(self) -> None:
        errores = validate_purchase_cost_decisions(
            [PurchaseCostDecision(context_id=_CTX, base=BASE_APLICAR)],
            {_CTX: _MAPEO_PELADO},
            {_CTX: "Compras marzo"},
        )
        _motivo, texto = errores[0]
        assert "Compras marzo" in texto
        assert "Descuento de la línea" in texto, "no el nombre técnico del campo"
        assert "discount" not in texto


class TestElDefaultSeguroNoPuedeSerMudo:
    def test_mapear_un_descuento_sin_declarar_la_base_avisa(self) -> None:
        aviso = hojas_que_necesitan_aviso([], {_CTX: _MAPEO_COMPLETO})
        assert "discount" in aviso[_CTX]
        assert "taxes" in aviso[_CTX]

    def test_el_flete_de_linea_ya_no_entra_en_este_aviso(self) -> None:
        """F-H6.e le dio consumidor en los DOS modos: con `al_costo` entra al
        costo del producto y con `gasto_aparte` se registra como gasto de
        logística. Deja de ser un no-op, igual que `shipping_cost` (abajo)."""
        solo_linea = {**_MAPEO_PELADO, "flete_linea": "shipping_cost_line"}
        assert hojas_que_necesitan_aviso([], {_CTX: solo_linea}) == {}

    def test_declarar_la_base_apaga_el_aviso_de_los_ajustes(self) -> None:
        aviso = hojas_que_necesitan_aviso(
            [PurchaseCostDecision(context_id=_CTX, base=BASE_APLICAR)],
            {_CTX: _MAPEO_COMPLETO},
        )
        assert aviso == {}

    def test_una_hoja_sin_columnas_de_costo_no_avisa_nada(self) -> None:
        assert hojas_que_necesitan_aviso([], {_CTX: _MAPEO_PELADO}) == {}

    def test_el_flete_del_comprobante_no_entra_en_este_aviso(self) -> None:
        """`shipping_cost` SÍ tiene consumidor desde F-H6.b: se cobra como gasto
        de logística aunque no se distribuya. No es un no-op."""
        solo_envio = {**_MAPEO_PELADO, "envio": "shipping_cost"}
        assert hojas_que_necesitan_aviso([], {_CTX: solo_envio}) == {}

    def test_el_texto_dice_que_hacer(self) -> None:
        texto = texto_del_aviso("Compras marzo", ["discount"])
        assert "Compras marzo" in texto
        assert "Descuento de la línea" in texto
        assert "no modificaron el costo" in texto


class TestUnAjusteVacioYUnoIlegibleNoSonLoMismo:
    """La regla que `_parse_amount` no puede dar: acá el CERO es un valor válido.

    Ese parser devuelve `None` para vacío, para ilegible y para todo lo que no sea
    positivo, porque nació para montos de operación. Reusarlo convertiría una
    celda con texto en «sin descuento», que es perder un dato sin que nadie se
    entere.
    """

    def test_una_celda_vacia_es_cero(self) -> None:
        assert parse_ajuste(None) == Decimal("0")
        assert parse_ajuste("") == Decimal("0")
        assert parse_ajuste("   ") == Decimal("0")

    def test_un_cero_declarado_tambien_es_cero_y_es_valido(self) -> None:
        assert parse_ajuste("0") == Decimal("0")
        assert parse_ajuste(0) == Decimal("0")

    def test_texto_ilegible_no_se_convierte_en_cero(self) -> None:
        assert parse_ajuste("ver factura") == AJUSTE_ILEGIBLE
        assert parse_ajuste("s/d") == AJUSTE_ILEGIBLE
        assert parse_ajuste("--") == AJUSTE_ILEGIBLE

    def test_lee_el_formato_de_una_planilla_argentina(self) -> None:
        assert parse_ajuste("$ 1.234,56") == Decimal("1234.56")
        assert parse_ajuste("1,50") == Decimal("1.50")
        assert parse_ajuste("2,000.75") == Decimal("2000.75")

    def test_un_negativo_se_lee_tal_cual(self) -> None:
        """No se decide acá si tiene sentido: eso lo resuelve la aritmética, que ya
        reporta cuando el descuento se come el monto entero."""
        assert parse_ajuste("-500") == Decimal("-500")

    def test_el_aviso_dice_la_hoja_la_columna_y_cuantas_filas(self) -> None:
        texto = texto_del_ajuste_ilegible("Compras marzo", "Bonificación", 3)
        assert "Compras marzo" in texto
        assert "Bonificación" in texto
        assert "3 celdas" in texto
        assert "SIN ese ajuste" in texto

    def test_y_concuerda_en_singular(self) -> None:
        assert "1 celda que" in texto_del_ajuste_ilegible("Compras", "IVA", 1)


class TestLaReconciliacionDelCosto:
    """Paso 6 — la suma de las partes tiene que explicar el total aplicado.

    Sin esto, un costo final es un número que hay que creer. Con esto, cualquiera
    puede rehacer la cuenta con lo que el archivo trajo.
    """

    def test_base_mas_ajustes_mas_fletes_da_el_total_de_cada_linea(self) -> None:
        from app.domain.purchase_cost import CostLine, build_line_costs

        lineas = [
            CostLine(
                row_index=0,
                amount=Decimal("12000"),
                quantity=10,
                discount=Decimal("2000"),
                taxes=Decimal("500"),
                shipping_line=Decimal("300"),
            ),
            CostLine(
                row_index=1,
                amount=Decimal("8000"),
                quantity=4,
                discount=Decimal("0"),
                taxes=Decimal("0"),
                shipping_line=Decimal("100"),
            ),
        ]
        plan = build_line_costs(
            lineas,
            shared_shipping=Decimal("900"),
            shared_mode=COMPARTIDO_SUBTOTAL,
            line_mode=LINEA_AL_COSTO,
            basis=BASE_APLICAR,
        )

        for original, calculada in zip(lineas, plan.lines, strict=True):
            esperado = (
                original.amount
                - original.discount
                + original.taxes
                + original.shipping_line
                + calculada.shipping_allocated
            )
            assert calculada.total == esperado, f"fila {original.row_index}"

    def test_lo_repartido_suma_exactamente_la_cifra_compartida(self) -> None:
        """Incluido el redondeo: tres líneas iguales sobre $10 dan 3,33 × 3 = 9,99,
        y el centavo que falta tiene que estar en alguna línea, no perdido."""
        from app.domain.purchase_cost import CostLine, build_line_costs

        lineas = [
            CostLine(row_index=i, amount=Decimal("100"), quantity=1) for i in range(3)
        ]
        plan = build_line_costs(
            lineas, shared_shipping=Decimal("10"), shared_mode=COMPARTIDO_SUBTOTAL
        )
        assert sum(línea.shipping_allocated for línea in plan.lines) == Decimal("10")
        assert plan.repartido == Decimal("10")
        assert plan.sin_repartir == Decimal("0")

    def test_el_costo_unitario_es_el_total_dividido_la_cantidad(self) -> None:
        from app.domain.purchase_cost import CostLine, build_line_costs

        plan = build_line_costs(
            [CostLine(row_index=0, amount=Decimal("12000"), quantity=10)],
        )
        línea = plan.lines[0]
        assert línea.unit_cost_final == (línea.total / 10).quantize(Decimal("0.01"))
