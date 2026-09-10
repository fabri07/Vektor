"""Qué cuenta como identidad fuerte, y sobre todo qué NO.

La mitad valiosa de estos tests es la negativa: cada caso donde el módulo se
NIEGA a producir clave es un caso donde, sin esa negativa, Véktor descartaría
plata real creyendo que ya la tenía.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.domain.operation_identity import (
    CAMPOS_DE_IDENTIDAD,
    MOTIVO_EMISOR_SIN_IDENTIFICADOR,
    MOTIVO_NUMERO_FALTANTE,
    MOTIVO_SERIE_FALTANTE,
    MOTIVO_SIN_COLUMNAS,
    MOTIVO_SISTEMA_NO_DECLARADO,
    MOTIVO_TIPO_DESCONOCIDO,
    MOTIVO_TIPO_NO_DECLARADO,
    ClaveDeOperacion,
    LineaEfectiva,
    SinClave,
    clave_de_operacion,
    hay_columnas_de_identidad,
    huella_de_contenido,
    leer_numero_de_comprobante,
    normalizar_serie,
    normalizar_tipo_de_comprobante,
)

# Mapeo de una hoja de compras completa: todas las columnas de identidad mapeadas.
COLS_COMPRA = {
    "invoice_number": "comprobante",
    "document_type": "tipo",
    "document_series": "punto_venta",
    "supplier_cuit": "cuit_prov",
    "supplier_name": "proveedor",
    "amount": "total",
    "expense_date": "fecha",
}

COLS_VENTA = {
    "invoice_number": "comprobante",
    "document_type": "tipo",
    "document_series": "punto_venta",
    "customer_cuit": "cuit_cliente",
    "customer_name": "cliente",
    "amount": "total",
    "transaction_date": "fecha",
}


def fila_compra(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "comprobante": "00000123",
        "tipo": "Factura A",
        "punto_venta": "0001",
        "cuit_prov": "30-71234567-8",
        "proveedor": "Distribuidora San Juan",
        "total": "12000",
        "fecha": "2026-03-04",
    }
    base.update(over)
    return base


# ── Lo que SÍ produce clave ──────────────────────────────────────────────────
def test_compra_con_comprobante_completo_tiene_clave() -> None:
    clave = clave_de_operacion(fila_compra(), COLS_COMPRA, "expense")
    assert isinstance(clave, ClaveDeOperacion)
    assert clave.granularidad == "documento"
    # El CUIT normalizado y el tipo canónico entran en la clave: es lo que la
    # hace comparable entre dos exportaciones distintas del mismo documento.
    assert "30712345678" in clave.clave
    assert ":factura:" in clave.clave


def test_el_mismo_comprobante_escrito_distinto_da_la_misma_clave() -> None:
    """Dos exportadores escriben el mismo documento de dos formas. Si eso da dos
    claves, la deduplicación no ve nada — que es como si no existiera."""
    a = clave_de_operacion(fila_compra(), COLS_COMPRA, "expense")
    b = clave_de_operacion(
        fila_compra(
            comprobante="0001-00000123",
            tipo="FC A",
            punto_venta="1",
            cuit_prov="30712345678",
        ),
        COLS_COMPRA,
        "expense",
    )
    assert isinstance(a, ClaveDeOperacion)
    assert isinstance(b, ClaveDeOperacion)
    assert a.clave == b.clave


def test_la_letra_fiscal_no_distingue_documentos() -> None:
    """Factura A y Factura B con mismo emisor, punto de venta y número no pueden
    coexistir: la letra es el régimen impositivo, no el documento."""
    a = clave_de_operacion(fila_compra(tipo="Factura A"), COLS_COMPRA, "expense")
    b = clave_de_operacion(fila_compra(tipo="Factura B"), COLS_COMPRA, "expense")
    assert isinstance(a, ClaveDeOperacion) and isinstance(b, ClaveDeOperacion)
    assert a.clave == b.clave


def test_id_externo_con_sistema_declarado_gana_sobre_el_comprobante() -> None:
    cols = {**COLS_COMPRA, "external_operation_id": "id", "external_source": "sistema"}
    clave = clave_de_operacion(
        fila_compra(id="OP-1024", sistema="Tango"), cols, "expense"
    )
    assert isinstance(clave, ClaveDeOperacion)
    assert clave.clave.startswith("k1:ext:tango:op-1024")


def test_id_de_linea_explicito_baja_la_granularidad_a_linea() -> None:
    cols = {**COLS_COMPRA, "external_line_id": "renglon"}
    clave = clave_de_operacion(fila_compra(renglon="3"), cols, "expense")
    assert isinstance(clave, ClaveDeOperacion)
    assert clave.granularidad == "linea"
    assert clave.clave.endswith("#3")


# ── Lo que NO produce clave (el corazón del módulo) ──────────────────────────
def test_id_externo_sin_sistema_declarado_no_es_clave_fuerte() -> None:
    """El "1024" de un sistema y el "1024" de otro son operaciones distintas."""
    cols = {**COLS_COMPRA, "external_operation_id": "id"}
    r = clave_de_operacion(fila_compra(id="1024"), cols, "expense")
    assert r == SinClave(MOTIVO_SISTEMA_NO_DECLARADO)


def test_compra_sin_cuit_del_proveedor_no_es_clave_fuerte() -> None:
    """El nombre NO identifica: se escribe de mil formas y se repite entre
    empresas. Sin CUIT el documento queda como candidato, no como duplicado."""
    r = clave_de_operacion(fila_compra(cuit_prov=""), COLS_COMPRA, "expense")
    assert r == SinClave(MOTIVO_EMISOR_SIN_IDENTIFICADOR)


def test_un_nombre_de_proveedor_no_reemplaza_al_cuit() -> None:
    """Aunque el nombre esté mapeado y presente: sigue sin haber clave."""
    cols = {k: v for k, v in COLS_COMPRA.items() if k != "supplier_cuit"}
    r = clave_de_operacion(fila_compra(), cols, "expense")
    assert r == SinClave(MOTIVO_EMISOR_SIN_IDENTIFICADOR)


def test_cuit_de_longitud_invalida_no_es_identificador() -> None:
    r = clave_de_operacion(fila_compra(cuit_prov="3071234"), COLS_COMPRA, "expense")
    assert r == SinClave(MOTIVO_EMISOR_SIN_IDENTIFICADOR)


def test_sin_tipo_declarado_no_hay_clave() -> None:
    """El mismo número existe en tipos distintos: factura 0001-123 y remito
    0001-123 del mismo proveedor son dos documentos."""
    r = clave_de_operacion(fila_compra(tipo=""), COLS_COMPRA, "expense")
    assert r == SinClave(MOTIVO_TIPO_NO_DECLARADO)


def test_tipo_fuera_del_catalogo_no_hay_clave() -> None:
    r = clave_de_operacion(fila_compra(tipo="Comprobante interno X"), COLS_COMPRA, "expense")
    assert r == SinClave(MOTIVO_TIPO_DESCONOCIDO)


def test_sin_punto_de_venta_no_hay_clave_en_un_formato_que_lo_exige() -> None:
    """El número se reinicia por punto de venta: sin él, "00000123" identifica
    tantas facturas como bocas de facturación tenga el emisor."""
    r = clave_de_operacion(fila_compra(punto_venta=""), COLS_COMPRA, "expense")
    assert r == SinClave(MOTIVO_SERIE_FALTANTE)


def test_sin_numero_no_hay_clave() -> None:
    r = clave_de_operacion(fila_compra(comprobante=""), COLS_COMPRA, "expense")
    assert r == SinClave(MOTIVO_NUMERO_FALTANTE)


def test_archivo_sin_columnas_de_identidad() -> None:
    r = clave_de_operacion({"total": "100"}, {"amount": "total"}, "expense")
    assert r == SinClave(MOTIVO_SIN_COLUMNAS)


def test_columna_no_mapeada_no_se_adivina() -> None:
    """La fila TRAE el número, pero la columna no está mapeada a
    `invoice_number`. Adivinar cuál es el comprobante es adivinar identidad."""
    r = clave_de_operacion(
        {"nro": "0001-123", "tipo": "Factura", "total": "100"},
        {"amount": "total", "document_type": "tipo"},
        "expense",
    )
    assert r == SinClave(MOTIVO_NUMERO_FALTANTE)


# ── El emisor de una venta propia no es el cliente ───────────────────────────
def test_en_una_venta_el_emisor_es_el_negocio_no_el_cliente() -> None:
    """El cliente es el RECEPTOR del comprobante. Dos facturas propias con el
    mismo punto de venta y número no pueden existir, tengan el cliente que
    tengan: si la clave dependiera del cliente, cambiar el cliente de una
    factura ya importada la haría entrar de nuevo."""
    a = clave_de_operacion(
        {**fila_compra(), "cuit_cliente": "27-11111111-4", "cliente": "Ana"},
        COLS_VENTA,
        "sale",
    )
    b = clave_de_operacion(
        {**fila_compra(), "cuit_cliente": "20-22222222-9", "cliente": "Beto"},
        COLS_VENTA,
        "sale",
    )
    assert isinstance(a, ClaveDeOperacion) and isinstance(b, ClaveDeOperacion)
    assert a.clave == b.clave
    assert "propio" in a.clave


def test_una_venta_no_necesita_cuit_del_cliente_para_tener_clave() -> None:
    r = clave_de_operacion({**fila_compra(), "cuit_cliente": ""}, COLS_VENTA, "sale")
    assert isinstance(r, ClaveDeOperacion)


def test_venta_y_compra_con_el_mismo_numero_no_colisionan() -> None:
    venta = clave_de_operacion(fila_compra(), COLS_VENTA, "sale")
    compra = clave_de_operacion(fila_compra(), COLS_COMPRA, "expense")
    assert isinstance(venta, ClaveDeOperacion) and isinstance(compra, ClaveDeOperacion)
    assert venta.clave != compra.clave


def test_dos_proveedores_con_el_mismo_numero_no_colisionan() -> None:
    a = clave_de_operacion(fila_compra(), COLS_COMPRA, "expense")
    b = clave_de_operacion(fila_compra(cuit_prov="30-99999999-7"), COLS_COMPRA, "expense")
    assert isinstance(a, ClaveDeOperacion) and isinstance(b, ClaveDeOperacion)
    assert a.clave != b.clave


def test_dos_puntos_de_venta_con_el_mismo_numero_no_colisionan() -> None:
    a = clave_de_operacion(fila_compra(punto_venta="0001"), COLS_COMPRA, "expense")
    b = clave_de_operacion(fila_compra(punto_venta="0002"), COLS_COMPRA, "expense")
    assert isinstance(a, ClaveDeOperacion) and isinstance(b, ClaveDeOperacion)
    assert a.clave != b.clave


# ── Normalizadores ──────────────────────────────────────────────────────────
def test_el_numero_se_lee_separando_el_punto_de_venta() -> None:
    """El mismo comprobante se escribe entero en una columna o partido en dos.
    Si eso diera claves distintas, el archivo B no reconocería nada de lo que
    trajo el archivo A y la deduplicación no serviría para nada."""
    assert leer_numero_de_comprobante("0001-00000123") == ("1", "123")
    assert leer_numero_de_comprobante("00000123") == ("", "123")
    assert leer_numero_de_comprobante("A 0001-00000123") == ("1", "123")
    assert leer_numero_de_comprobante("") == ("", "")


def test_el_punto_de_venta_embebido_no_colisiona_con_otro() -> None:
    """Se recortan los ceros de cada componente, pero NO se pierde la separación:
    0001-123 y 0012-123 siguen siendo dos comprobantes."""
    assert leer_numero_de_comprobante("0001-00000123") != leer_numero_de_comprobante(
        "0012-00000123"
    )


def test_punto_de_venta_de_la_columna_que_contradice_al_del_numero() -> None:
    """Elegir uno de los dos sería inventar cuál de los dos comprobantes es."""
    from app.domain.operation_identity import MOTIVO_SERIE_CONTRADICTORIA

    r = clave_de_operacion(
        fila_compra(comprobante="0007-00000123", punto_venta="0001"),
        COLS_COMPRA,
        "expense",
    )
    assert r == SinClave(MOTIVO_SERIE_CONTRADICTORIA)


def test_el_punto_de_venta_puede_venir_solo_adentro_del_numero() -> None:
    """Sin columna propia de punto de venta, pero con el formato completo en el
    número: hay clave. Leer `PPPP-NNNNNNNN` es parsear el formato, no adivinar."""
    cols = {k: v for k, v in COLS_COMPRA.items() if k != "document_series"}
    r = clave_de_operacion(fila_compra(comprobante="0001-00000123"), cols, "expense")
    assert isinstance(r, ClaveDeOperacion)


def test_la_serie_si_recorta_ceros_porque_es_un_entero() -> None:
    """`0001` y `1` son el mismo punto de venta escrito con distinto padding."""
    assert normalizar_serie("0001") == normalizar_serie("1") == "1"
    assert normalizar_serie("0000") == "0"


def test_alias_de_tipo() -> None:
    assert normalizar_tipo_de_comprobante("Remito") == "remito"
    assert normalizar_tipo_de_comprobante("N/C") is None  # no está en el catálogo
    assert normalizar_tipo_de_comprobante("Nota de Crédito") == "nota_credito"
    assert normalizar_tipo_de_comprobante("") is None


# ── Huella de contenido ─────────────────────────────────────────────────────
def _linea(monto: str, prod: str = "Coca 500", qty: int = 2) -> LineaEfectiva:
    return LineaEfectiva(
        monto=Decimal(monto),
        fecha=date(2026, 3, 4),
        cantidad=qty,
        precio_unitario=Decimal(monto) / qty,
        producto=prod,
    )


def test_reordenar_los_renglones_no_cambia_el_documento() -> None:
    a = huella_de_contenido([_linea("100"), _linea("200", "Fanta")])
    b = huella_de_contenido([_linea("200", "Fanta"), _linea("100")])
    assert a == b


def test_dos_renglones_identicos_no_se_colapsan() -> None:
    """Un remito con dos renglones iguales legítimos no es lo mismo que uno con
    un solo renglón. Colapsarlos borraría plata."""
    uno = huella_de_contenido([_linea("100")])
    dos = huella_de_contenido([_linea("100"), _linea("100")])
    assert uno != dos


def test_la_escala_del_importe_no_cambia_la_huella() -> None:
    """`1500`, `1500.0` y `1500.00` son el mismo importe. Si dieran huellas
    distintas, el mismo documento reexportado se vería como conflicto."""
    base = LineaEfectiva(Decimal("1500"), date(2026, 3, 4), 1, None)
    con_ceros = LineaEfectiva(Decimal("1500.00"), date(2026, 3, 4), 1, None)
    assert huella_de_contenido([base]) == huella_de_contenido([con_ceros])


def test_la_hora_no_entra_en_la_huella() -> None:
    dia = LineaEfectiva(Decimal("100"), date(2026, 3, 4), 1, None)
    con_hora = LineaEfectiva(Decimal("100"), datetime(2026, 3, 4, 17, 30), 1, None)
    assert huella_de_contenido([dia]) == huella_de_contenido([con_hora])


def test_un_monto_distinto_cambia_la_huella() -> None:
    assert huella_de_contenido([_linea("100")]) != huella_de_contenido([_linea("101")])


def test_un_producto_distinto_con_el_mismo_monto_cambia_la_huella() -> None:
    """Dos remitos con los mismos totales y productos distintos no son el mismo
    documento."""
    assert huella_de_contenido([_linea("100", "Coca")]) != huella_de_contenido(
        [_linea("100", "Fanta")]
    )


def test_un_descuento_distinto_cambia_la_huella() -> None:
    sin = LineaEfectiva(Decimal("100"), date(2026, 3, 4), 1, None)
    con = LineaEfectiva(Decimal("100"), date(2026, 3, 4), 1, None, descuento=Decimal("5"))
    assert huella_de_contenido([sin]) != huella_de_contenido([con])


def test_la_huella_lleva_su_version_adelante() -> None:
    """Sin la versión adentro, cambiar la representación se vería como "todo es
    nuevo" sin ninguna señal de por qué."""
    assert huella_de_contenido([_linea("100")]).startswith("c1:")


# ── Compuerta barata ────────────────────────────────────────────────────────
def test_sin_ancla_mapeada_no_se_paga_la_pasada_de_identidad() -> None:
    assert not hay_columnas_de_identidad({"amount": "total", "document_type": "tipo"})
    assert hay_columnas_de_identidad({"invoice_number": "comprobante"})
    assert hay_columnas_de_identidad({"external_operation_id": "id"})


def test_los_campos_de_identidad_son_campos_canonicos_reales() -> None:
    """Si un campo de identidad no está en el catálogo, el usuario no lo puede
    mapear y la clave nunca se arma — el módulo quedaría muerto en silencio."""
    from app.application.services.column_mapping_service import CANONICAL_FIELDS

    for campo in CAMPOS_DE_IDENTIDAD:
        assert campo in CANONICAL_FIELDS["expense"] or campo in CANONICAL_FIELDS["sale"], (
            f"{campo} no existe en CANONICAL_FIELDS: no hay forma de mapearlo"
        )
