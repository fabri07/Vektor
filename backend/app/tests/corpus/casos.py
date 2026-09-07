"""Corpus versionado de ingestión: archivos y el resultado que se espera de ellos.

Qué es y qué NO es
------------------
Cada caso es un archivo real —lo arma openpyxl o se escribe byte a byte, y lo
parsea el parser de producción— junto con **lo que un contador esperaría que
quede en la base**. La expectativa no se copió de una corrida: se escribió a
mano leyendo el archivo, que es la única forma de que el corpus pueda detectar
que el motor cambió de opinión. Un baseline generado por el propio motor sólo
prueba que el motor sigue haciendo lo que hace.

Por eso cada caso lleva ``porque``: la decisión de negocio que fija. Si un caso
falla, lo primero que hay que preguntarse no es "¿qué test hay que actualizar?"
sino "¿cambiamos esta decisión a propósito?".

``REVISADO_EN`` versiona el corpus. Cambiar una expectativa exige subir esa
versión en el caso y decir por qué en el commit: es un cambio de política sobre
plata, no un ajuste de test.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from openpyxl import Workbook

#: Versión del corpus. Se sube cuando una expectativa cambia (no cuando se agrega
#: un caso nuevo, que no toca ninguna decisión ya revisada).
REVISADO_EN = "E4-2026-09"

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CSV_MIME = "text/csv"
_CLIENTE = "Consumidor final"


@dataclass(frozen=True)
class VentaEsperada:
    fecha: date
    monto: Decimal
    cantidad: int


@dataclass(frozen=True)
class ProductoEsperado:
    nombre: str
    stock: int
    costo: Decimal | None
    precio: Decimal | None


@dataclass(frozen=True)
class CasoDeCorpus:
    nombre: str
    #: La decisión de negocio que fija este caso. Se lee cuando el caso falla.
    porque: str
    revisado_en: str
    contenido: bytes
    mime: str
    archivo: str
    mapeo: list[dict[str, Any]]
    confirmado: dict[str, bool]
    ventas: list[VentaEsperada] = field(default_factory=list)
    productos: list[ProductoEsperado] = field(default_factory=list)
    #: Filas que NO se importan y quedan para revisar. Es parte de la expectativa:
    #: un caso que espera 0 y captura 3 está perdiendo datos en silencio.
    otros: int = 0


def _xlsx(hojas: dict[str, list[list[Any]]]) -> bytes:
    libro = Workbook()
    libro.remove(libro.active)
    for titulo, filas in hojas.items():
        hoja = libro.create_sheet(titulo)
        for fila in filas:
            hoja.append(fila)
    buffer = io.BytesIO()
    libro.save(buffer)
    return buffer.getvalue()


def _mapeo(pares: dict[str, str], contexto: str | None = None) -> list[dict[str, Any]]:
    salida: list[dict[str, Any]] = []
    for origen, destino in pares.items():
        entrada: dict[str, Any] = {"source_column": origen, "target_field": destino}
        if contexto is not None:
            entrada["context_id"] = contexto
            entrada["entity_type"] = "sale" if contexto.endswith("Ventas") else "product"
        salida.append(entrada)
    return salida


_VENTAS_HEADERS = ["fecha", "producto", "cantidad", "total", "cliente", "forma de pago"]
_MAPEO_VENTAS = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "cliente": "customer_name",
    "forma de pago": "payment_method",
}
_CATALOGO_HEADERS = ["producto", "stock", "precio de venta", "precio de compra"]
_MAPEO_CATALOGO = {
    "producto": "name",
    "stock": "stock_units",
    "precio de venta": "sale_price_ars",
    "precio de compra": "unit_cost_ars",
}


def _csv(lineas: list[str], encoding: str = "utf-8", sep: str = ",") -> bytes:
    return "\n".join(sep.join(campos) for campos in [ln.split("|") for ln in lineas]).encode(
        encoding
    )


CASOS: list[CasoDeCorpus] = [
    CasoDeCorpus(
        nombre="ventas_csv_punto_y_coma_formato_ar",
        porque=(
            "El CSV con `;` es lo que exporta Excel en configuración regional "
            "argentina, y sus montos vienen con punto de miles y coma decimal. "
            "`12.500,50` son doce mil quinientos con cincuenta, no doce con cinco."
        ),
        revisado_en=REVISADO_EN,
        contenido=_csv(
            [
                "fecha|producto|cantidad|total|cliente|forma de pago",
                "05/03/2024|Vela aromatica|2|12.500,50|Consumidor final|efectivo",
                "06/03/2024|Difusor|1|8.400|Consumidor final|transferencia",
            ],
            sep=";",
        ),
        mime=_CSV_MIME,
        archivo="ventas_ar.csv",
        mapeo=_mapeo(_MAPEO_VENTAS),
        confirmado={"ventas": True},
        ventas=[
            VentaEsperada(date(2024, 3, 5), Decimal("12500.50"), 2),
            VentaEsperada(date(2024, 3, 6), Decimal("8400"), 1),
        ],
    ),
    CasoDeCorpus(
        nombre="ventas_csv_latin1_con_acentos",
        porque=(
            "Un CSV guardado en latin-1 —lo que sale de un sistema viejo— no puede "
            "perder los acentos del nombre del producto: el nombre es la clave con "
            "la que después se lo matchea contra el catálogo."
        ),
        revisado_en=REVISADO_EN,
        contenido=_csv(
            [
                "fecha|producto|cantidad|total|cliente|forma de pago",
                "05/03/2024|Café molido|1|3.400|Consumidor final|efectivo",
            ],
            encoding="latin-1",
        ),
        mime=_CSV_MIME,
        archivo="ventas_latin1.csv",
        mapeo=_mapeo(_MAPEO_VENTAS),
        confirmado={"ventas": True},
        ventas=[VentaEsperada(date(2024, 3, 5), Decimal("3400"), 1)],
    ),
    CasoDeCorpus(
        nombre="ventas_xlsx_exportado_en_formato_us",
        porque=(
            "Una planilla exportada en formato US trae `04/13/2026` (que sólo se "
            "lee mm/dd) y `03/04/2026` (que se lee de las dos formas). La primera "
            "fija el orden de la columna: la segunda es el 4 de MARZO. Los montos, "
            "por lo mismo, son `1,234.56` = mil doscientos treinta y cuatro."
        ),
        revisado_en=REVISADO_EN,
        contenido=_xlsx(
            {
                "Ventas": [
                    _VENTAS_HEADERS,
                    ["04/13/2026", "Vela aromatica", 1, "1,234.56", _CLIENTE, "efectivo"],
                    ["03/04/2026", "Difusor", 2, "2,000.00", _CLIENTE, "efectivo"],
                ]
            }
        ),
        mime=_XLSX_MIME,
        archivo="ventas_us.xlsx",
        mapeo=_mapeo(_MAPEO_VENTAS),
        confirmado={"ventas": True},
        ventas=[
            VentaEsperada(date(2026, 3, 4), Decimal("2000.00"), 2),
            VentaEsperada(date(2026, 4, 13), Decimal("1234.56"), 1),
        ],
    ),
    CasoDeCorpus(
        nombre="ventas_con_celdas_que_no_se_pueden_leer",
        porque=(
            "Las tres formas de perder plata en silencio, en un archivo donde el "
            "resto está bien: una escala incompatible con su columna (`12.50` entre "
            "miles con punto), una cantidad fraccionaria y una negativa. Ninguna se "
            "importa y ninguna desaparece: las tres quedan en Otros con el original."
        ),
        revisado_en=REVISADO_EN,
        contenido=_xlsx(
            {
                "Ventas": [
                    _VENTAS_HEADERS,
                    ["05/03/2024", "Vela aromatica", 1, "12.500,50", _CLIENTE, "efectivo"],
                    ["06/03/2024", "Difusor", 1, "12.50", _CLIENTE, "efectivo"],
                    ["07/03/2024", "Portarretrato", 2.5, "8.400", _CLIENTE, "efectivo"],
                    ["08/03/2024", "Sahumerio", -3, "1.200", _CLIENTE, "efectivo"],
                ]
            }
        ),
        mime=_XLSX_MIME,
        archivo="ventas_sucias.xlsx",
        mapeo=_mapeo(_MAPEO_VENTAS),
        confirmado={"ventas": True},
        ventas=[VentaEsperada(date(2024, 3, 5), Decimal("12500.50"), 1)],
        otros=3,
    ),
    CasoDeCorpus(
        nombre="ventas_xlsx_con_serial_de_excel",
        porque=(
            "Una celda de fecha sin formato viaja como número de días desde "
            "1899-12-30. Es una fecha escrita como la escribe Excel, no un dato "
            "ilegible: 45123 es el 16/07/2023."
        ),
        revisado_en=REVISADO_EN,
        contenido=_xlsx(
            {"Ventas": [_VENTAS_HEADERS, [45123, "Vela aromatica", 1, 8400, _CLIENTE, "efectivo"]]}
        ),
        mime=_XLSX_MIME,
        archivo="ventas_serial.xlsx",
        mapeo=_mapeo(_MAPEO_VENTAS),
        confirmado={"ventas": True},
        ventas=[VentaEsperada(date(2023, 7, 16), Decimal("8400"), 1)],
    ),
    CasoDeCorpus(
        nombre="catalogo_xlsx_con_stock_de_apertura",
        porque=(
            "Un catálogo declara identidades y saldos, no movimientos: crea los "
            "productos con su stock inicial y sus dos precios, y no toca la caja. "
            "El costo `1.200` de una columna sin decimales son mil doscientos."
        ),
        revisado_en=REVISADO_EN,
        contenido=_xlsx(
            {
                "Catalogo": [
                    _CATALOGO_HEADERS,
                    ["Vela aromatica", 10, "2.100", "1.200"],
                    ["Difusor bambu", 4, "3.500", "1.900"],
                ]
            }
        ),
        mime=_XLSX_MIME,
        archivo="catalogo.xlsx",
        mapeo=_mapeo(_MAPEO_CATALOGO),
        confirmado={"productos": True},
        productos=[
            ProductoEsperado("Difusor bambu", 4, Decimal("1900"), Decimal("3500")),
            ProductoEsperado("Vela aromatica", 10, Decimal("1200"), Decimal("2100")),
        ],
    ),
]
