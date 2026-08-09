"""F-M — de (entidad, concepto, calificadores) al campo, con tres resultados.

La regla rectora: si Véktor no puede demostrar qué quiso decir el usuario,
conserva el dato y pregunta. Nunca lo transforma en silencio en otro concepto
contable — que es lo que convertía un flete en un precio de compra.
"""

from __future__ import annotations

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

    def test_bonificacion_proveedor_no_es_el_nombre_del_proveedor(self) -> None:
        r = _leer("Bonificación proveedor", "expense")
        assert r.outcome == "sin_evidencia"
        assert r.concept == "descuento"

    def test_descuento_por_producto_no_es_el_nombre_del_producto(self) -> None:
        r = _leer("Descuento por producto", "expense")
        assert r.concept == "descuento"
        assert r.target is None

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
    def test_la_fecha_de_una_planilla_de_gastos_es_una_fecha(self) -> None:
        assert _leer("Fecha del gasto", "expense").target == "expense_date"

    def test_la_fecha_de_una_planilla_de_ventas_tambien(self) -> None:
        assert _leer("Fecha de venta", "sale").target == "transaction_date"

    def test_el_codigo_de_barras_no_entra_al_campo_sku(self) -> None:
        """El SKU es identidad de producto (F2/F5): meterle un código de barras
        fusiona o duplica productos."""
        assert _leer("Código de barras", "product").target == "barcode"

    def test_una_descripcion_no_es_un_nombre(self) -> None:
        assert _leer("Descripción", "product").target == "description"


class TestLoQueYaAndabaSigueAndando:
    def test_los_tres_precios_de_asteria_no_colisionan(self) -> None:
        headers = ["Precio de compra", "Precio de lista", "Precio de venta final"]
        targets = [_leer(h, "product").target for h in headers]
        assert targets == ["unit_cost_ars", "list_price_ars", "sale_price_ars"]
        assert len(set(targets)) == 3

    def test_el_mismo_header_va_a_campos_distintos_segun_la_hoja(self) -> None:
        assert _leer("Precio unitario", "sale").target == "unit_price"
        assert _leer("Precio unitario", "product").target == "unit_cost_ars"

    def test_los_headers_comunes(self) -> None:
        assert _leer("Forma de pago", "expense").target == "payment_method"
        assert _leer("Monto", "sale").target == "amount"
        assert _leer("Cantidad", "sale").target == "quantity"
        assert _leer("Stock", "product").target == "stock_units"
        assert _leer("Proveedor", "expense").target == "supplier_name"
        assert _leer("DNI", "customer").target == "dni"
        assert _leer("DNI", "sale").target == "customer_dni"


class TestLosAcentosDejanDeSerUnAgujero:
    def test_los_headers_con_tilde_resuelven(self) -> None:
        assert _leer("Envío", "expense").target == "shipping_cost"
        assert _leer("Artículo", "sale").target == "product_name"
        assert _leer("Categoría", "expense").target == "category"
        assert _leer("Método de pago", "sale").target == "payment_method"
        assert _leer("Teléfono", "customer").target == "phone"


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
