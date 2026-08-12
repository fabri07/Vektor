"""F-F.4: el efecto de inventario de cada hoja, derivado de lo que la hoja contiene.

Hasta F-F.3 este módulo garantizaba lo contrario de lo que garantiza ahora: que
Véktor **nunca** decidiera solo aplicar la historia de un archivo sobre el stock
(``test_historical_replay_nunca_es_un_default``). Esa regla nació del incidente
don pedro —un archivo de 10.931 ventas movió el inventario entero en una
confirmación y la parte ya contada en el saldo de apertura se descontó dos
veces— y se levanta porque las dos condiciones que faltaban ya existen:

1. **El ancla del catálogo entra antes de todos los eventos**, no como un
   movimiento datado. Compuerta:
   ``test_inventory_projection.py::test_el_catalogo_declara_un_absoluto_y_pisa_el_saldo_previo``.
2. **El replay ordena por fecha**, así que una compra posterior no respalda una
   venta anterior. Compuerta:
   ``test_inventory_replay_gate.py::test_una_compra_posterior_no_respalda_la_venta_anterior``.

**Si alguna de esas dos se rompe, este default tiene que volver atrás.** Están
nombradas acá, y no sólo descriptas, para que se puedan ir a leer.

Lo que este módulo garantiza hoy: que toda compra y venta de mercadería mueva el
inventario, y que una hoja que no habla de unidades no tenga efecto **ninguno**
—ni siquiera uno que diga "no toco nada"—, porque de ahí salía el cartel «Estas
cantidades no afectan el inventario» en hojas de clientes y de gastos fijos.
"""

from __future__ import annotations

import pytest

from app.domain.inventory_effect import (
    CURRENT_SNAPSHOT,
    HISTORICAL_REPLAY,
    LEGACY_EFFECTS,
    VALID_EFFECTS,
    InvalidInventoryEffectError,
    SheetInventoryProfile,
    default_effect_for,
    discard_legacy_overrides,
    options_for,
    resolve_inventory_effects,
)


def _hoja(
    context_id: str = "sheet:1",
    entity: str | None = "sale",
    campos: tuple[str, ...] = ("product_name", "quantity", "amount"),
) -> SheetInventoryProfile:
    return SheetInventoryProfile(
        context_id=context_id, entity=entity, mapped_fields=frozenset(campos)
    )


class TestDefaults:
    def test_toda_compra_o_venta_de_mercaderia_mueve_el_inventario(self) -> None:
        """El invariante de F-F.4, sobre TODA combinación de hoja.

        Es el inverso exacto del que había hasta F-F.3: entonces se recorría la
        misma matriz para comprobar que ninguna combinación propusiera aplicar el
        histórico. Ahora se recorre para comprobar que ninguna combinación de
        mercadería se quede sin mover stock.
        """
        combinaciones = (
            ("product_name", "quantity"),
            ("sku", "quantity"),
            ("barcode", "quantity"),
            ("product_name", "quantity", "amount", "transaction_date"),
        )
        for entidad in ("sale", "expense"):
            for campos in combinaciones:
                efecto = default_effect_for(_hoja(entity=entidad, campos=campos))
                assert efecto == HISTORICAL_REPLAY, (
                    f"entidad={entidad} campos={campos} no mueve inventario"
                )

    def test_el_efecto_derivado_siempre_es_valido_o_ausente(self) -> None:
        """Ninguna combinación puede producir un modo que el importador no honre."""
        entidades = ("sale", "expense", "product", "customer", "supplier", None, "otro")
        combinaciones = (
            (),
            ("amount",),
            ("quantity",),
            ("product_name",),
            ("sku", "quantity"),
            ("barcode", "stock_units"),
            ("product_name", "quantity", "amount", "transaction_date"),
        )
        for entidad in entidades:
            for campos in combinaciones:
                efecto = default_effect_for(_hoja(entity=entidad, campos=campos))
                assert efecto is None or efecto in VALID_EFFECTS

    @pytest.mark.parametrize(
        ("entidad", "campos", "esperado"),
        [
            # Compra y venta de mercadería: el stock se mueve. Es la función.
            pytest.param(
                "sale",
                ("product_name", "quantity", "amount"),
                HISTORICAL_REPLAY,
                id="test_las_ventas_de_mercaderia_descuentan",
            ),
            pytest.param(
                "expense",
                ("product_name", "quantity", "amount"),
                HISTORICAL_REPLAY,
                id="test_las_compras_de_mercaderia_suman",
            ),
            # Un catálogo es una FOTO del stock de hoy, no una secuencia.
            pytest.param(
                "product",
                ("name", "stock_units"),
                CURRENT_SNAPSHOT,
                id="test_catalogo_con_stock_declara_saldo_absoluto",
            ),
            # Catálogo SIN cantidad: no dice cuánto hay, así que no habla de stock.
            pytest.param(
                "product",
                ("name", "sale_price_ars"),
                None,
                id="test_lista_de_precios_no_habla_de_inventario",
            ),
            # Servicios, honorarios, resumen diario: hay monto, no hay unidades.
            pytest.param(
                "sale",
                ("amount", "transaction_date"),
                None,
                id="test_venta_sin_producto_no_habla_de_inventario",
            ),
            pytest.param(
                "sale",
                ("product_name", "amount"),
                None,
                id="test_venta_con_producto_pero_sin_cantidad_no_habla_de_inventario",
            ),
        ],
    )
    def test_efecto_por_hoja(
        self, entidad: str, campos: tuple[str, ...], esperado: str | None
    ) -> None:
        assert default_effect_for(_hoja(entity=entidad, campos=campos)) == esperado

    def test_los_maestros_no_tienen_efecto(self) -> None:
        """`None`, no un modo que diga "no toco nada".

        La diferencia es visible: de un modo salía el cartel «Estas cantidades no
        afectan el inventario» en la hoja de Clientes, que es una respuesta a una
        pregunta que esa hoja nunca hizo.
        """
        for entidad in ("customer", "supplier"):
            assert default_effect_for(_hoja(entity=entidad, campos=("name",))) is None


class TestResolucion:
    def test_las_hojas_con_efecto_lo_declaran_y_las_otras_no_aparecen(self) -> None:
        """La hoja sin efecto se OMITE del dict, no mapea a un valor.

        Es lo que hace que "sin dato" deje de ser ambiguo aguas abajo: hasta
        F-F.3 significaba a la vez «el caller no mandó el modo» y «esta hoja no
        habla de inventario».
        """
        resuelto = resolve_inventory_effects(
            [
                _hoja("sheet:ventas", "sale"),
                _hoja("sheet:catalogo", "product", ("name", "stock_units")),
                _hoja("sheet:clientes", "customer", ("name",)),
            ]
        )
        assert resuelto == {
            "sheet:ventas": HISTORICAL_REPLAY,
            "sheet:catalogo": CURRENT_SNAPSHOT,
        }
        assert "sheet:clientes" not in resuelto

    def test_un_override_que_coincide_es_un_no_op(self) -> None:
        """Sobrevive por compatibilidad de API: ya no puede cambiar nada."""
        resuelto = resolve_inventory_effects(
            [_hoja("sheet:ventas", "sale")], {"sheet:ventas": HISTORICAL_REPLAY}
        )
        assert resuelto["sheet:ventas"] == HISTORICAL_REPLAY

    def test_un_override_que_contradice_el_contenido_se_rechaza(self) -> None:
        """Declarar que una hoja de ventas es una foto del stock leería un
        movimiento como si fuera un saldo. Antes entraba sin 422 —el dominio
        validaba el valor y la hoja, nunca la combinación."""
        with pytest.raises(InvalidInventoryEffectError):
            resolve_inventory_effects(
                [_hoja("sheet:ventas", "sale")], {"sheet:ventas": CURRENT_SNAPSHOT}
            )

    def test_un_override_sobre_una_hoja_sin_efecto_se_rechaza(self) -> None:
        """La hoja existe, pero no habla de inventario: pedirle un efecto es
        creer haber decidido algo que no va a pasar."""
        with pytest.raises(InvalidInventoryEffectError):
            resolve_inventory_effects(
                [_hoja("sheet:clientes", "customer", ("name",))],
                {"sheet:clientes": HISTORICAL_REPLAY},
            )

    def test_un_modo_desconocido_se_rechaza(self) -> None:
        with pytest.raises(InvalidInventoryEffectError):
            resolve_inventory_effects([_hoja("sheet:ventas", "sale")], {"sheet:ventas": "replay"})

    def test_un_override_a_una_hoja_inexistente_se_rechaza(self) -> None:
        with pytest.raises(InvalidInventoryEffectError):
            resolve_inventory_effects(
                [_hoja("sheet:ventas", "sale")], {"sheet:fantasma": HISTORICAL_REPLAY}
            )

    def test_los_modos_eliminados_no_llegan_hasta_acá(self) -> None:
        """`resolve_inventory_effects` NO los tolera: los saca `discard_legacy_overrides`.

        Son dos situaciones distintas a propósito — un modo desconocido es un bug
        del cliente y tiene que explotar; uno legacy es un frontend viejo durante
        la ventana de deploy.
        """
        for modo in LEGACY_EFFECTS:
            with pytest.raises(InvalidInventoryEffectError):
                resolve_inventory_effects([_hoja("sheet:ventas", "sale")], {"sheet:ventas": modo})


class TestDescarteDeModosLegacy:
    """Railway y Vercel redespliegan en paralelo y sin orden garantizado."""

    def test_los_dos_modos_eliminados_se_descartan_y_se_reportan(self) -> None:
        limpios, descartados = discard_legacy_overrides(
            {"sheet:ventas": "informational", "sheet:clientes": "no_inventory"}
        )
        assert limpios == {}
        assert sorted(descartados) == ["sheet:clientes", "sheet:ventas"]

    def test_un_modo_vigente_no_se_descarta(self) -> None:
        limpios, descartados = discard_legacy_overrides({"sheet:ventas": HISTORICAL_REPLAY})
        assert limpios == {"sheet:ventas": HISTORICAL_REPLAY}
        assert descartados == []

    def test_un_modo_desconocido_pasa_para_que_lo_rechace_la_resolucion(self) -> None:
        """Descartar acá un typo lo convertiría en un import silencioso."""
        limpios, descartados = discard_legacy_overrides({"sheet:ventas": "replay"})
        assert limpios == {"sheet:ventas": "replay"}
        assert descartados == []

    def test_sin_overrides_no_hay_nada_que_descartar(self) -> None:
        assert discard_legacy_overrides(None) == ({}, [])
        assert discard_legacy_overrides({}) == ({}, [])


class TestLoQueLaPantallaMuestra:
    """`options_for` dejó de ofrecer y pasó a explicar."""

    def test_una_hoja_con_efecto_muestra_exactamente_ese_efecto(self) -> None:
        assert options_for(_hoja(entity="sale", campos=("product_name", "quantity"))) == [
            HISTORICAL_REPLAY
        ]
        assert options_for(_hoja(entity="product", campos=("name", "stock_units"))) == [
            CURRENT_SNAPSHOT
        ]

    def test_una_hoja_que_no_habla_de_inventario_no_muestra_nada(self) -> None:
        """Vacío, no una opción única con un cartel.

        Es el pedido textual: la hoja de Gastos_Fijos, la de Clientes y la de
        Proveedores no tienen por qué decir nada sobre el inventario.
        """
        assert options_for(_hoja(entity="sale", campos=("amount",))) == []
        assert options_for(_hoja(entity="customer", campos=("name",))) == []
        assert options_for(_hoja(entity="product", campos=("name",))) == []

    def test_la_cantidad_mapeada_es_lo_que_hace_que_la_hoja_mueva_stock(self) -> None:
        """La misma hoja cambia al mapear `quantity` — por eso el efecto se
        recalcula con el mapeo borrador y no una sola vez."""
        assert options_for(_hoja(entity="sale", campos=("product_name",))) == []
        assert options_for(_hoja(entity="sale", campos=("product_name", "quantity"))) == [
            HISTORICAL_REPLAY
        ]

    def test_lo_que_se_muestra_es_siempre_un_efecto_que_el_importador_honra(self) -> None:
        for perfil in (
            _hoja(entity="product", campos=("name", "stock_units")),
            _hoja(entity="sale", campos=("product_name", "quantity")),
            _hoja(entity="expense", campos=("product_name", "quantity")),
            _hoja(entity=None, campos=()),
        ):
            assert set(options_for(perfil)) <= VALID_EFFECTS
