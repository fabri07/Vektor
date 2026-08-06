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

    def test_ventas_historicas_calculan_sin_tocar_stock(self) -> None:
        assert default_effect_for(_hoja(entity="sale")) == INFORMATIONAL

    def test_compras_historicas_calculan_sin_tocar_stock(self) -> None:
        assert default_effect_for(_hoja(entity="expense")) == INFORMATIONAL

    def test_catalogo_con_stock_declara_saldo_absoluto(self) -> None:
        """Un catálogo es una FOTO del stock de hoy, no una secuencia."""
        assert (
            default_effect_for(_hoja(entity="product", campos=("name", "stock_units")))
            == CURRENT_SNAPSHOT
        )

    def test_lista_de_precios_no_declara_saldo(self) -> None:
        """Catálogo SIN cantidad: no dice cuánto hay, así que no toca inventario."""
        assert (
            default_effect_for(_hoja(entity="product", campos=("name", "sale_price_ars")))
            == NO_INVENTORY
        )

    def test_venta_sin_producto_no_toca_inventario(self) -> None:
        """Servicios, honorarios, resumen diario: hay monto, no hay unidades."""
        assert (
            default_effect_for(_hoja(entity="sale", campos=("amount", "transaction_date")))
            == NO_INVENTORY
        )

    def test_venta_con_producto_pero_sin_cantidad_no_toca_inventario(self) -> None:
        assert (
            default_effect_for(_hoja(entity="sale", campos=("product_name", "amount")))
            == NO_INVENTORY
        )

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
