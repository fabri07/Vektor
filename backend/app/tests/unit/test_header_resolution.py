"""F-M — de (entidad, concepto, calificadores) al campo, con tres resultados.

La regla rectora: si Véktor no puede demostrar qué quiso decir el usuario,
conserva el dato y pregunta. Nunca lo transforma en silencio en otro concepto
contable — que es lo que convertía un flete en un precio de compra.
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    RESOLUCION,
    _normalize_col,
    read_header,
)


def _leer(header: str, entity: str):  # noqa: ANN202 — el tipo real es HeaderReading
    return read_header(_normalize_col(header), entity)


class TestLosCincoEncabezadosDelProblema:
    """Ninguno puede terminar en un concepto contable distinto del suyo."""

    def test_envio_unitario_no_es_un_precio_de_compra(self) -> None:
        r = _leer("Envío unitario", "expense")
        assert r.outcome == "sin_evidencia"
        assert r.target is None
        # Y se explica: reconoció que es un envío, sólo que de una granularidad
        # que no tiene campo. No es lo mismo que "no entiendo esto".
        assert r.concept == "envio"
        assert "envío por unidad" in r.duda

    @pytest.mark.parametrize(
        "header",
        [
            # F-M.7: desde que `discount` existe llega a destino. Lo que este caso
            # cuida sigue siendo lo mismo: que el calificador de entidad no se lleve
            # el campo (antes resolvía a `supplier_name`).
            pytest.param(
                "Bonificación proveedor",
                id="test_bonificacion_proveedor_no_es_el_nombre_del_proveedor",
            ),
            pytest.param(
                "Descuento por producto",
                id="test_descuento_por_producto_no_es_el_nombre_del_producto",
            ),
        ],
    )
    def test_el_calificador_de_entidad_no_se_lleva_el_descuento(self, header: str) -> None:
        r = _leer(header, "expense")
        assert r.concept == "descuento"
        assert r.target == "discount"

    def test_precio_con_iva_no_es_un_impuesto(self) -> None:
        r = _leer("Precio con IVA", "expense")
        assert r.outcome == "ambiguo"
        assert set(r.options) == {"unit_price", "amount"}
        assert r.duda

    def test_total_factura_sin_impuestos_no_es_el_monto_de_la_linea(self) -> None:
        r = _leer("Total factura sin impuestos", "expense")
        assert r.outcome == "sin_evidencia"
        assert "comprobante" in r.duda

    def test_costo_final_por_producto_pregunta_en_vez_de_elegir(self) -> None:
        r = _leer("Costo final por producto", "expense")
        assert r.outcome == "ambiguo"
        assert set(r.options) == {"unit_price", "amount"}


class TestLosEmpatesQueResolviaElOrdenDelDict:
    @pytest.mark.parametrize(
        ("header", "entidad", "target"),
        [
            pytest.param(
                "Fecha del gasto",
                "expense",
                "expense_date",
                id="test_la_fecha_de_una_planilla_de_gastos_es_una_fecha",
            ),
            pytest.param(
                "Fecha de venta",
                "sale",
                "transaction_date",
                id="test_la_fecha_de_una_planilla_de_ventas_tambien",
            ),
            # El SKU es identidad de producto (F2/F5): meterle un código de barras
            # fusiona o duplica productos.
            pytest.param(
                "Código de barras",
                "product",
                "barcode",
                id="test_el_codigo_de_barras_no_entra_al_campo_sku",
            ),
            pytest.param(
                "Descripción",
                "product",
                "description",
                id="test_una_descripcion_no_es_un_nombre",
            ),
        ],
    )
    def test_el_header_resuelve_al_campo_de_su_entidad(
        self, header: str, entidad: str, target: str
    ) -> None:
        assert _leer(header, entidad).target == target


class TestLoQueYaAndabaSigueAndando:
    def test_los_tres_precios_de_asteria_no_colisionan(self) -> None:
        headers = ["Precio de compra", "Precio de lista", "Precio de venta final"]
        targets = [_leer(h, "product").target for h in headers]
        assert targets == ["unit_cost_ars", "list_price_ars", "sale_price_ars"]
        assert len(set(targets)) == 3

    @pytest.mark.parametrize(
        ("entidad", "target"),
        [("sale", "unit_price"), ("product", "unit_cost_ars")],
    )
    def test_el_mismo_header_va_a_campos_distintos_segun_la_hoja(
        self, entidad: str, target: str
    ) -> None:
        assert _leer("Precio unitario", entidad).target == target

    @pytest.mark.parametrize(
        ("header", "entidad", "target"),
        [
            ("Forma de pago", "expense", "payment_method"),
            ("Monto", "sale", "amount"),
            ("Cantidad", "sale", "quantity"),
            ("Stock", "product", "stock_units"),
            ("Proveedor", "expense", "supplier_name"),
            ("DNI", "customer", "dni"),
            ("DNI", "sale", "customer_dni"),
        ],
    )
    def test_los_headers_comunes(self, header: str, entidad: str, target: str) -> None:
        assert _leer(header, entidad).target == target


class TestLosAcentosDejanDeSerUnAgujero:
    @pytest.mark.parametrize(
        ("header", "entidad", "target"),
        [
            ("Envío", "expense", "shipping_cost"),
            ("Artículo", "sale", "product_name"),
            ("Categoría", "expense", "category"),
            ("Método de pago", "sale", "payment_method"),
            ("Teléfono", "customer", "phone"),
        ],
    )
    def test_los_headers_con_tilde_resuelven(self, header: str, entidad: str, target: str) -> None:
        assert _leer(header, entidad).target == target


class TestSinEvidencia:
    def test_un_header_desconocido_no_inventa_nada(self) -> None:
        r = _leer("xyz_desconocido_123", "sale")
        assert r.outcome == "sin_evidencia"
        assert r.target is None
        # Sin duda: no hay nada que explicar, no se reconoció el concepto.
        assert r.duda is None

    def test_un_concepto_sin_campo_en_esa_hoja_lo_dice(self) -> None:
        # Una hoja de ventas no tiene dónde poner un envío.
        r = _leer("Flete", "sale")
        assert r.outcome == "sin_evidencia"
        assert r.concept == "envio"
        assert r.duda

    def test_la_marca_se_declara_como_campo_propio(self) -> None:
        r = _leer("Marca", "product")
        assert r.outcome == "sin_evidencia"
        assert "campo propio" in r.duda
        # Y dice por qué no es un proveedor: es la Reforma de Proveedores.
        assert "proveedor" in r.duda


class TestLaLecturaConservaLoQueElHeaderDecia:
    """`qualifiers` no cambia la decisión —esa ya la tomó la regla— pero es lo
    único con lo que se puede explicar. Sin él, «Envío unitario» y «Envío» dan el
    mismo mensaje en pantalla, y son cosas distintas."""

    def test_el_calificador_viaja_con_la_lectura_que_no_resuelve(self) -> None:
        r = _leer("Envío unitario", "expense")
        assert r.outcome == "sin_evidencia"
        assert "unitario" in r.qualifiers

    def test_tambien_en_la_lectura_que_si_resuelve(self) -> None:
        r = _leer("Precio de compra", "product")
        assert r.target == "unit_cost_ars"
        assert "de_compra" in r.qualifiers

    def test_un_header_sin_calificadores_no_inventa_ninguno(self) -> None:
        assert _leer("Monto", "sale").qualifiers == frozenset()


class TestLaTablaEsCoherenteConElCatalogo:
    def test_todo_target_existe_en_su_entidad(self) -> None:
        """Un typo manda una columna a un campo inexistente, y el import la
        descarta sin que nada avise."""
        for entidad, conceptos in RESOLUCION.items():
            for concepto, reglas in conceptos.items():
                for regla in reglas:
                    candidatos = [regla.target] if regla.target else list(regla.opciones)
                    for t in candidatos:
                        assert t in CANONICAL_FIELDS[entidad], (
                            f"{entidad}.{concepto} → {t} no está en CANONICAL_FIELDS"
                        )

    def test_toda_regla_sin_target_explica_la_duda(self) -> None:
        """Una columna que no se mapea y no dice por qué es la peor de las tres
        salidas: el usuario ve un hueco sin saber qué hacer."""
        for entidad, conceptos in RESOLUCION.items():
            for concepto, reglas in conceptos.items():
                for regla in reglas:
                    if regla.target is None:
                        assert regla.duda, f"{entidad}.{concepto} no explica la duda"

    def test_un_ambiguo_ofrece_al_menos_dos_opciones(self) -> None:
        for entidad, conceptos in RESOLUCION.items():
            for concepto, reglas in conceptos.items():
                for regla in reglas:
                    if regla.opciones:
                        assert len(regla.opciones) >= 2, f"{entidad}.{concepto}"


class TestLosPadronesDeMaestros:
    """Las dos entidades que la batería original no cubría — el hueco que dejó
    pasar una rama que rompía el import de clientes y el de proveedores.

    Se afirma sobre `read_header`, no sobre la cadena: si sólo se probara el
    resultado final, fuzzy rescataría estos encabezados y el test seguiría verde
    con la tabla vacía.
    """

    @pytest.mark.parametrize(
        ("header", "entidad"),
        [
            pytest.param(
                "Cliente",
                "customer",
                id="test_cliente_en_un_padron_de_clientes_es_el_nombre",
            ),
            pytest.param(
                "Proveedor",
                "supplier",
                id="test_proveedor_en_un_padron_de_proveedores_es_el_nombre",
            ),
        ],
    )
    def test_la_entidad_en_su_propio_padron_es_el_nombre(self, header: str, entidad: str) -> None:
        r = _leer(header, entidad)
        assert r.outcome == "unico"
        assert r.target == "name"

    @pytest.mark.parametrize(
        ("header", "entidad", "target"),
        [
            pytest.param(
                "Tipo cliente",
                "customer",
                "customer_type",
                id="test_pero_tipo_cliente_es_su_clasificacion",
            ),
            pytest.param(
                "Condición de pago",
                "supplier",
                "payment_method",
                id="test_la_condicion_de_pago_de_un_proveedor_es_el_metodo",
            ),
        ],
    )
    def test_el_calificador_manda_a_otro_campo(
        self, header: str, entidad: str, target: str
    ) -> None:
        assert _leer(header, entidad).target == target

    @pytest.mark.parametrize("header", ["IVA", "Condición IVA", "Situación IVA"])
    def test_el_iva_de_una_persona_es_su_condicion_fiscal_no_un_monto(self, header: str) -> None:
        assert _leer(header, "customer").target == "iva_condition"


class TestCompraYVentaNombranCualDeLosTresPrecios:
    @pytest.mark.parametrize(
        ("header", "target"),
        [("Compra", "unit_cost_ars"), ("Venta", "sale_price_ars")],
    )
    def test_en_un_catalogo_compra_es_el_costo_y_venta_el_precio(
        self, header: str, target: str
    ) -> None:
        assert _leer(header, "product").target == target

    def test_un_monto_pelado_no_dice_cual_de_los_tres_es(self) -> None:
        """Adivinarlo es el bug que F10 cerró: no hay regla, y está bien."""
        assert _leer("Importe", "product").target is None


class TestLosTresCostosDeUnaCompra:
    """F-M.7 — descuento, impuestos y el flete YA asignado a la línea.

    Los tres los entendía el reconocedor desde el principio y no tenían dónde ir:
    la aritmética existía (`domain/purchase_cost.py`) y el campo no.
    """

    @pytest.mark.parametrize("header", ["Bonificación proveedor", "Descuento"])
    def test_un_descuento_es_un_descuento_aunque_nombre_al_proveedor(self, header: str) -> None:
        assert _leer(header, "expense").target == "discount"

    @pytest.mark.parametrize("header", ["IVA", "Impuestos"])
    def test_un_impuesto_de_la_linea_tiene_campo_propio(self, header: str) -> None:
        assert _leer(header, "expense").target == "taxes"

    def test_pero_un_precio_con_iva_sigue_siendo_un_precio(self) -> None:
        """El caso que da nombre a la fase. `con`/`sin` declaran inclusión: si al
        existir `taxes` esta columna empezara a resolver ahí, el precio de la
        línea entraría como impuesto — exactamente el bug que se vino a cerrar."""
        r = _leer("Precio con IVA", "expense")
        assert r.outcome == "ambiguo"
        assert r.target != "taxes"
        assert set(r.options) == {"amount", "unit_price"}

    @pytest.mark.parametrize("header", ["Flete por línea", "Envío prorrateado"])
    def test_el_flete_por_linea_no_es_el_del_comprobante(self, header: str) -> None:
        """Semántica OPUESTA y por eso son campos distintos: el del comprobante se
        cobra una vez (la cifra repetida se colapsa) y éste se SUMA, porque el
        reparto ya lo hizo quien armó la planilla."""
        assert _leer(header, "expense").target == "shipping_cost_line"

    @pytest.mark.parametrize("header", ["Envío", "Flete"])
    def test_envio_a_secas_sigue_siendo_el_del_comprobante(self, header: str) -> None:
        """Decisión declarada: no se vuelve ambiguo. F-H6.b ya pregunta la
        granularidad cuando la hoja no trae comprobante, y ahí el número está a la
        vista. Preguntarlo dos veces es fricción en el header más común."""
        assert _leer(header, "expense").target == "shipping_cost"

    def test_y_el_flete_por_unidad_sigue_sin_tener_campo(self) -> None:
        """Tres granularidades no son dos: Véktor lee la del comprobante y la de
        línea, no la de cada unidad. Sigue explicándose en vez de elegir una."""
        r = _leer("Envío unitario", "expense")
        assert r.outcome == "sin_evidencia"
        assert r.concept == "envio"
        assert r.duda
