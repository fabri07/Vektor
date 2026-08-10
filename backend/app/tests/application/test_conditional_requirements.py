"""F-C.c3: "obligatorio" es contextual, y describirlo no puede volverlo bloqueante.

`required: bool` contesta una pregunta sola para todos los archivos y por eso
contesta mal en los dos sentidos: dice que el monto de una venta es obligatorio
cuando la planilla trae precio × cantidad, y no dice nada del producto en una hoja
que sí mueve inventario. `CONDITIONAL_REQUIREMENTS` lo DESCRIBE.

Dos cosas se clavan acá y las dos ya se rompieron antes en este repo:

1. **La condición no se copia.** Los conjuntos que definen "esta hoja mueve
   unidades" viven en `app/domain/inventory_effect.py`. Los tests comparan por
   IDENTIDAD (``is``), no por contenido: una copia con los mismos elementos pasa
   un ``==`` y después las dos definiciones se van cada una por su lado sin que
   nada avise. Es el mismo modo de falla del catálogo de campos duplicado en el
   frontend (incidente ASTERIA).
2. **F-C no rechaza nada nuevo.** Volver bloqueante "producto si la venta es
   inventariable" rechazaría con 422 toda planilla de servicios u honorarios que
   hoy importa bien.
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    CONDITIONAL_REQUIREMENTS,
    COVERED_BY_ALTERNATIVE,
    REQUIRED_ALTERNATIVES,
    REQUIRED_FIELDS,
    SHEET_MOVES_UNITS,
    conditional_requirement,
    missing_required_fields,
    requirement_applies,
)
from app.domain.inventory_effect import _PRODUCT_FIELDS, _QUANTITY_FIELDS

_CONDICIONALES = sorted(
    (entidad, campo)
    for entidad, campos in CONDITIONAL_REQUIREMENTS.items()
    for campo in campos
)
_POR_INVENTARIO = [
    (entidad, campo)
    for entidad, campo in _CONDICIONALES
    if CONDITIONAL_REQUIREMENTS[entidad][campo].condition == SHEET_MOVES_UNITS
]


class TestLaCondicionNoSeCopia:
    """Anti-divergencia: los conjuntos son los del dominio, no unos iguales."""

    @pytest.mark.parametrize(("entidad", "campo"), _POR_INVENTARIO)
    def test_las_senales_son_los_objetos_del_dominio(self, entidad: str, campo: str) -> None:
        signals = CONDITIONAL_REQUIREMENTS[entidad][campo].signals
        assert any(grupo is _PRODUCT_FIELDS for grupo in signals), (
            f"«{entidad}.{campo}» no apunta al `_PRODUCT_FIELDS` del dominio sino a "
            "otro objeto: es una copia, y dentro de dos PRs va a decir otra cosa."
        )
        assert any(grupo is _QUANTITY_FIELDS for grupo in signals), (
            f"«{entidad}.{campo}» no apunta al `_QUANTITY_FIELDS` del dominio."
        )

    def test_la_alternativa_del_monto_no_se_reescribe(self) -> None:
        """`{unit_price, quantity}` ya vive en `REQUIRED_ALTERNATIVES`.

        Escribirla también en `signals` sería la tercera copia (la cuarta, con la
        del frontend) de la misma regla.
        """
        for entidad, campos in CONDITIONAL_REQUIREMENTS.items():
            for campo, req in campos.items():
                if req.condition != COVERED_BY_ALTERNATIVE:
                    continue
                assert req.signals == (), (
                    f"«{entidad}.{campo}» copió la alternativa en `signals`."
                )
                assert REQUIRED_ALTERNATIVES.get(entidad, {}).get(campo), (
                    f"«{entidad}.{campo}» declara que una alternativa lo cubre y no "
                    "hay ninguna en REQUIRED_ALTERNATIVES."
                )


class TestNadaSeVolvioBloqueante:
    """La compuerta del confirm sigue siendo la de antes de F-C."""

    def test_los_requeridos_no_crecieron(self) -> None:
        assert REQUIRED_FIELDS["sale"] == ["amount", "transaction_date"]
        assert REQUIRED_FIELDS["expense"] == ["amount", "expense_date"]

    @pytest.mark.parametrize(("entidad", "campo"), _POR_INVENTARIO)
    def test_un_condicional_de_inventario_nunca_es_un_requerido(
        self, entidad: str, campo: str
    ) -> None:
        assert campo not in REQUIRED_FIELDS.get(entidad, []), (
            f"«{entidad}.{campo}» pasó a REQUIRED_FIELDS: eso rechaza con 422 toda "
            "planilla de servicios que hoy importa bien."
        )

    def test_una_hoja_de_servicios_sigue_pasando_la_validacion(self) -> None:
        """Honorarios: monto y fecha, sin producto ni cantidad. Importa igual."""
        assert missing_required_fields("sale", {"amount", "transaction_date"}) == set()


class TestElMontoLoCubreLaAlternativa:
    @pytest.mark.parametrize("entidad", ["sale", "expense"])
    def test_sin_nada_mapeado_el_monto_hace_falta(self, entidad: str) -> None:
        assert requirement_applies(entidad, "amount", set()) is True

    @pytest.mark.parametrize("entidad", ["sale", "expense"])
    def test_con_precio_unitario_y_cantidad_el_monto_no_hace_falta(
        self, entidad: str
    ) -> None:
        assert requirement_applies(entidad, "amount", {"unit_price", "quantity"}) is False

    @pytest.mark.parametrize("entidad", ["sale", "expense"])
    def test_media_alternativa_no_alcanza(self, entidad: str) -> None:
        """Con el precio solo no hay nada que multiplicar."""
        assert requirement_applies(entidad, "amount", {"unit_price"}) is True


class TestElInventarioDecideProductoYCantidad:
    """`moves_units` = identifica un producto Y trae cantidad. La mitad que falta
    es la que se pide; en una hoja que no habla de productos no se pide ninguna."""

    _SERVICIOS = {"amount", "transaction_date"}

    def test_una_hoja_de_servicios_no_pide_producto_ni_cantidad(self) -> None:
        assert requirement_applies("sale", "product_name", self._SERVICIOS) is False
        assert requirement_applies("sale", "quantity", self._SERVICIOS) is False

    def test_con_producto_mapeado_falta_la_cantidad(self) -> None:
        mapeado = self._SERVICIOS | {"product_name"}
        assert requirement_applies("sale", "quantity", mapeado) is True

    def test_con_cantidad_mapeada_falta_el_producto(self) -> None:
        mapeado = self._SERVICIOS | {"quantity"}
        assert requirement_applies("sale", "product_name", mapeado) is True

    def test_con_las_dos_mitades_no_falta_ninguna(self) -> None:
        mapeado = self._SERVICIOS | {"product_name", "quantity"}
        assert requirement_applies("sale", "product_name", mapeado) is False
        assert requirement_applies("sale", "quantity", mapeado) is False

    def test_el_sku_tambien_identifica_al_producto(self) -> None:
        """La señal es el conjunto del dominio, no el campo `product_name`.

        Una hoja de compras que trae SKU y cantidad ya mueve unidades: pedirle
        además el nombre sería inventar un requisito que el importador no tiene.
        """
        mapeado = {"amount", "expense_date", "sku", "quantity"}
        assert requirement_applies("expense", "product_name", mapeado) is False

    def test_una_hoja_de_compras_con_producto_y_sin_cantidad_pide_la_cantidad(
        self,
    ) -> None:
        mapeado = {"amount", "expense_date", "product_name"}
        assert requirement_applies("expense", "quantity", mapeado) is True


class TestSinReglaContextualMandaElBooleanoDeSiempre:
    @pytest.mark.parametrize(
        ("entidad", "campo", "esperado"),
        [
            ("sale", "transaction_date", True),
            ("expense", "expense_date", True),
            ("product", "name", True),
            ("customer", "name", True),
            ("supplier", "name", True),
            ("sale", "notes", False),
            ("product", "category", False),
        ],
    )
    def test_el_booleano_de_siempre(self, entidad: str, campo: str, esperado: bool) -> None:
        assert conditional_requirement(entidad, campo) is None
        assert requirement_applies(entidad, campo, set()) is esperado


class TestCadaReglaSeExplicaYApuntaAUnCampoReal:
    @pytest.mark.parametrize(("entidad", "campo"), _CONDICIONALES)
    def test_el_campo_existe_en_su_entidad(self, entidad: str, campo: str) -> None:
        assert campo in CANONICAL_FIELDS[entidad]

    @pytest.mark.parametrize(("entidad", "campo"), _CONDICIONALES)
    def test_la_regla_se_explica_en_castellano(self, entidad: str, campo: str) -> None:
        explicacion = CONDITIONAL_REQUIREMENTS[entidad][campo].explanation
        assert len(explicacion.split()) >= 10, (
            f"La regla de «{entidad}.{campo}» no alcanza a explicar CUÁNDO aplica."
        )
