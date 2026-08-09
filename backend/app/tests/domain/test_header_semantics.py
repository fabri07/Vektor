"""F-M — separar el concepto de un encabezado de lo que sólo lo modifica.

Lo que se prueba: que un calificador NUNCA sea la respuesta, y que dos núcleos
de verdad no se desempaten solos. El motor anterior no tenía ninguna de las dos
nociones y por eso «Envío unitario» resolvía a precio de compra y «Fecha del
gasto» al monto.
"""

from __future__ import annotations

from app.domain.header_semantics import analyze_header, tokenize


def _leer(header: str, entity: str = "") -> tuple[str | None, set[str], tuple[str, ...]]:
    """Header crudo → (concepto, calificadores, rivales), como lo ve el importador."""
    normalizado = header.lower().strip().replace(" ", "_").replace("-", "_")
    a = analyze_header(normalizado, entity)
    return a.concept, set(a.qualifiers), a.rivals


class TestTokenizacion:
    def test_saca_acentos(self) -> None:
        # Los vocabularios están escritos sin tilde. Sin esto «Envío» no matchea
        # NADA: el problema no era el target equivocado, era la ausencia total de
        # sugerencia.
        assert tokenize("envío") == ("envio",)
        assert tokenize("categoría") == ("categoria",)
        assert tokenize("bonificación") == ("bonificacion",)

    def test_descarta_preposiciones_vacias(self) -> None:
        assert tokenize("fecha_de_la_venta") == ("fecha", "venta")

    def test_conserva_con_y_sin(self) -> None:
        # No son relleno: son las que distinguen un precio de un impuesto.
        assert tokenize("precio_con_iva") == ("precio", "con", "iva")
        assert tokenize("precio_sin_iva") == ("precio", "sin", "iva")


class TestUnCalificadorNuncaEsLaRespuesta:
    def test_envio_unitario_sigue_siendo_envio(self) -> None:
        """El caso que corrompe datos: `unitario`(8) le ganaba a `envio`(5) y el
        flete entraba como precio de compra del producto."""
        concepto, cal, _ = _leer("Envío unitario", "expense")
        assert concepto == "envio"
        assert "unitario" in cal

    def test_bonificacion_proveedor_sigue_siendo_descuento(self) -> None:
        concepto, cal, _ = _leer("Bonificación proveedor", "expense")
        assert concepto == "descuento"
        assert "de_proveedor" in cal

    def test_descuento_por_producto_sigue_siendo_descuento(self) -> None:
        concepto, cal, _ = _leer("Descuento por producto", "expense")
        assert concepto == "descuento"
        assert "de_producto" in cal

    def test_nombre_del_proveedor_sigue_siendo_un_nombre(self) -> None:
        concepto, cal, _ = _leer("Nombre del proveedor", "expense")
        assert concepto == "nombre"
        assert "de_proveedor" in cal


class TestInclusionFiscal:
    def test_precio_con_iva_es_un_precio(self) -> None:
        """`iva` era el único match del header y se lo llevaba entero."""
        concepto, cal, _ = _leer("Precio con IVA", "expense")
        assert concepto == "precio"
        assert "con_impuesto" in cal

    def test_precio_sin_iva_tambien(self) -> None:
        concepto, cal, _ = _leer("Precio sin IVA", "expense")
        assert concepto == "precio"
        assert "sin_impuesto" in cal

    def test_iva_solo_si_es_un_impuesto(self) -> None:
        # Control: sin la preposición adelante, `IVA` sí nombra la columna.
        concepto, _, _ = _leer("IVA", "expense")
        assert concepto == "impuesto"

    def test_total_factura_sin_impuestos_es_un_monto_del_comprobante(self) -> None:
        concepto, cal, _ = _leer("Total factura sin impuestos", "expense")
        assert concepto == "monto"
        assert cal == {"por_comprobante", "sin_impuesto"}


class TestPalabrasDeOperacionYEntidad:
    def test_fecha_del_gasto_es_una_fecha(self) -> None:
        """El header más común de una planilla de gastos. `gasto`(5) y `fecha`(5)
        empataban y ganaba el orden del dict: la columna de FECHA entraba como
        monto."""
        concepto, cal, _ = _leer("Fecha del gasto", "expense")
        assert concepto == "fecha"
        assert "de_gasto" in cal

    def test_fecha_de_venta_tambien(self) -> None:
        concepto, cal, _ = _leer("Fecha de venta", "sale")
        assert concepto == "fecha"
        assert "de_venta" in cal

    def test_gasto_solo_si_es_el_monto(self) -> None:
        # Sin otro núcleo, la palabra de operación SÍ nombra la columna.
        concepto, _, _ = _leer("Gasto", "expense")
        assert concepto == "monto"

    def test_una_entidad_nunca_le_gana_a_una_palabra_de_monto(self) -> None:
        """«Total factura» es un monto del comprobante, no un comprobante."""
        concepto, cal, _ = _leer("Total del comprobante", "expense")
        assert concepto == "monto"
        assert "por_comprobante" in cal

    def test_la_entidad_sola_si_es_el_concepto(self) -> None:
        concepto, _, _ = _leer("Proveedor", "expense")
        assert concepto == "proveedor"


class TestEspecializacion:
    def test_codigo_de_barras_es_un_barcode_no_una_rivalidad(self) -> None:
        """`código`(6) y `barras`(6) empataban: el código de barras entraba al
        campo SKU, que es identidad de producto (F2/F5)."""
        concepto, _, rivales = _leer("Código de barras", "product")
        assert concepto == "barcode"
        assert rivales == ()

    def test_fecha_de_vencimiento_es_un_vencimiento(self) -> None:
        concepto, _, rivales = _leer("Fecha de vencimiento", "product")
        assert concepto == "vencimiento"
        assert rivales == ()

    def test_fecha_de_nacimiento_es_un_cumpleanos(self) -> None:
        concepto, _, _ = _leer("Fecha de nacimiento", "customer")
        assert concepto == "cumpleanos"

    def test_el_genero_solo_sigue_siendo_el_genero(self) -> None:
        # Control: sin el específico al lado, `fecha` y `código` son ellos mismos.
        assert _leer("Fecha", "expense")[0] == "fecha"
        assert _leer("Código", "product")[0] == "codigo"


class TestDosNucleosNoSeDesempatan:
    def test_dos_conceptos_sin_relacion_quedan_rivales(self) -> None:
        """No se elige. Elegir es exactamente lo que hacía el desempate por orden
        de declaración del dict."""
        concepto, _, rivales = _leer("Descuento e impuestos", "expense")
        assert concepto is None
        assert set(rivales) == {"descuento", "impuesto"}

    def test_un_header_desconocido_no_inventa_concepto(self) -> None:
        concepto, _, rivales = _leer("xyz_desconocido_123", "sale")
        assert concepto is None
        assert rivales == ()


class TestBigramas:
    def test_forma_pago_es_un_concepto_y_no_dos(self) -> None:
        concepto, _, _ = _leer("Forma de pago", "expense")
        assert concepto == "metodo_pago"

    def test_codigo_de_producto_es_el_sku(self) -> None:
        # Hoy `producto`(8) le gana a `codigo`(6) y resuelve al NOMBRE.
        concepto, _, _ = _leer("Código de producto", "product")
        assert concepto == "sku"


class TestElMismoTokenSegunLaHoja:
    def test_costo_en_un_catalogo_es_un_precio(self) -> None:
        assert _leer("Costo", "product")[0] == "precio"

    def test_costo_en_un_libro_de_compras_es_el_monto_de_la_linea(self) -> None:
        assert _leer("Costo", "expense")[0] == "monto"
