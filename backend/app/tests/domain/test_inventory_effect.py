"""F-H3.a: el efecto de inventario que Véktor propone para cada hoja.

Lo que este módulo tiene que garantizar es una cosa sobre todo: **que Véktor
nunca decida solo aplicar la historia de un archivo sobre el stock**. El resto
son defaults razonables; ése es el que, si se rompe, mueve el inventario de un
negocio real sin que nadie lo haya pedido.
"""

from __future__ import annotations

import pytest

from app.domain.inventory_effect import (
    CURRENT_SNAPSHOT,
    HISTORICAL_REPLAY,
    INFORMATIONAL,
    NO_INVENTORY,
    VALID_EFFECTS,
    InvalidInventoryEffectError,
    SheetInventoryProfile,
    default_effect_for,
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
    def test_historical_replay_nunca_es_un_default(self) -> None:
        """El invariante de la fase, sobre TODA combinación de hoja.

        Un default `historical_replay` significa aplicar el histórico de un
        archivo que puede estar incompleto o solaparse con saldos ya cargados.
        Se elige a mano, por hoja, o no se elige.
        """
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
                assert efecto != HISTORICAL_REPLAY, (
                    f"entidad={entidad} campos={campos} propuso aplicar el histórico"
                )
                assert efecto in VALID_EFFECTS

    @pytest.mark.parametrize(
        ("entidad", "campos", "esperado"),
        [
            # Ventas y compras históricas: se calculan, pero no mueven el stock.
            pytest.param(
                "sale",
                ("product_name", "quantity", "amount"),
                INFORMATIONAL,
                id="test_ventas_historicas_calculan_sin_tocar_stock",
            ),
            pytest.param(
                "expense",
                ("product_name", "quantity", "amount"),
                INFORMATIONAL,
                id="test_compras_historicas_calculan_sin_tocar_stock",
            ),
            # Un catálogo es una FOTO del stock de hoy, no una secuencia.
            pytest.param(
                "product",
                ("name", "stock_units"),
                CURRENT_SNAPSHOT,
                id="test_catalogo_con_stock_declara_saldo_absoluto",
            ),
            # Catálogo SIN cantidad: no dice cuánto hay, así que no toca inventario.
            pytest.param(
                "product",
                ("name", "sale_price_ars"),
                NO_INVENTORY,
                id="test_lista_de_precios_no_declara_saldo",
            ),
            # Servicios, honorarios, resumen diario: hay monto, no hay unidades.
            pytest.param(
                "sale",
                ("amount", "transaction_date"),
                NO_INVENTORY,
                id="test_venta_sin_producto_no_toca_inventario",
            ),
            pytest.param(
                "sale",
                ("product_name", "amount"),
                NO_INVENTORY,
                id="test_venta_con_producto_pero_sin_cantidad_no_toca_inventario",
            ),
        ],
    )
    def test_default_por_hoja(self, entidad: str, campos: tuple[str, ...], esperado: str) -> None:
        assert default_effect_for(_hoja(entity=entidad, campos=campos)) == esperado

    def test_los_maestros_no_tocan_inventario(self) -> None:
        for entidad in ("customer", "supplier"):
            assert default_effect_for(_hoja(entity=entidad, campos=("name",))) == NO_INVENTORY


class TestResolucion:
    def test_sin_overrides_devuelve_los_defaults(self) -> None:
        resuelto = resolve_inventory_effects(
            [
                _hoja("sheet:ventas", "sale"),
                _hoja("sheet:catalogo", "product", ("name", "stock_units")),
            ]
        )
        assert resuelto == {
            "sheet:ventas": INFORMATIONAL,
            "sheet:catalogo": CURRENT_SNAPSHOT,
        }

    def test_el_usuario_puede_elegir_el_replay_por_hoja(self) -> None:
        """El histórico no se resigna: queda a un clic, hoja por hoja."""
        resuelto = resolve_inventory_effects(
            [_hoja("sheet:ventas", "sale"), _hoja("sheet:compras", "expense")],
            {"sheet:ventas": HISTORICAL_REPLAY},
        )
        assert resuelto["sheet:ventas"] == HISTORICAL_REPLAY
        # La otra hoja NO se contagia: elegir el replay para una no lo elige para todas.
        assert resuelto["sheet:compras"] == INFORMATIONAL

    def test_un_modo_desconocido_se_rechaza(self) -> None:
        with pytest.raises(InvalidInventoryEffectError):
            resolve_inventory_effects([_hoja("sheet:ventas", "sale")], {"sheet:ventas": "replay"})

    def test_un_override_a_una_hoja_inexistente_se_rechaza(self) -> None:
        """No se ignora en silencio.

        El usuario cree haber decidido algo sobre el inventario; si esa hoja no
        existe, lo que decidió no va a pasar y tiene que enterarse ahora.
        """
        with pytest.raises(InvalidInventoryEffectError):
            resolve_inventory_effects(
                [_hoja("sheet:ventas", "sale")], {"sheet:fantasma": HISTORICAL_REPLAY}
            )


# ── F-H3.e: qué se le ofrece al usuario para cada hoja ──────────────────────


class TestOpcionesPorHoja:
    """`resolve_inventory_effects` valida el valor y la hoja, no la combinación.

    Ofrecer los cuatro modos en toda hoja entra sin 422 y hace cualquier cosa:
    `current_snapshot` en una hoja de ventas lee un movimiento como si fuera un
    saldo. Acotar es responsabilidad del dominio, no de la pantalla — una lista
    fija en la UI se desactualiza y termina ofreciendo lo que el importador no
    honra.
    """

    def test_el_default_siempre_esta_entre_las_opciones(self) -> None:
        """Si no, la pantalla no podría representar el estado en el que el archivo
        entra cuando el usuario no toca nada."""
        for perfil in (
            _hoja(entity="product", campos=("name", "stock_units")),
            _hoja(entity="product", campos=("name",)),
            _hoja(entity="sale", campos=("product_name", "quantity")),
            _hoja(entity="sale", campos=("amount",)),
            _hoja(entity="expense", campos=("product_name", "quantity")),
            _hoja(entity="customer", campos=("name",)),
            _hoja(entity=None, campos=()),
        ):
            assert default_effect_for(perfil) in options_for(perfil)

    def test_una_hoja_de_ventas_puede_aplicar_su_historia(self) -> None:
        opciones = options_for(_hoja(entity="sale", campos=("product_name", "quantity")))
        assert HISTORICAL_REPLAY in opciones
        # Un movimiento no declara un saldo absoluto.
        assert CURRENT_SNAPSHOT not in opciones

    def test_un_catalogo_no_ofrece_aplicar_la_historia(self) -> None:
        """Un saldo no es una secuencia de movimientos: no hay historia que aplicar."""
        opciones = options_for(_hoja(entity="product", campos=("name", "stock_units")))
        assert CURRENT_SNAPSHOT in opciones
        assert HISTORICAL_REPLAY not in opciones

    def test_una_hoja_que_no_mueve_unidades_no_ofrece_elegir(self) -> None:
        """Una venta de servicios (sin producto) o un maestro: no hay decisión."""
        assert options_for(_hoja(entity="sale", campos=("amount",))) == [NO_INVENTORY]
        assert options_for(_hoja(entity="customer", campos=("name",))) == [NO_INVENTORY]

    def test_la_cantidad_mapeada_es_lo_que_habilita_el_replay(self) -> None:
        """La misma hoja cambia de opciones al mapear `quantity` — por eso las
        opciones se recalculan con el mapeo borrador y no una sola vez."""
        sin_cantidad = options_for(_hoja(entity="sale", campos=("product_name",)))
        con_cantidad = options_for(_hoja(entity="sale", campos=("product_name", "quantity")))
        assert sin_cantidad == [NO_INVENTORY]
        assert HISTORICAL_REPLAY in con_cantidad

    def test_toda_opcion_ofrecida_es_un_efecto_valido(self) -> None:
        """Ofrecer algo que `resolve_inventory_effects` rechaza sería mandar al
        usuario derecho a un 422."""
        for perfil in (
            _hoja(entity="product", campos=("name", "stock_units")),
            _hoja(entity="sale", campos=("product_name", "quantity")),
            _hoja(entity="expense", campos=("product_name", "quantity")),
            _hoja(entity=None, campos=()),
        ):
            assert set(options_for(perfil)) <= VALID_EFFECTS
