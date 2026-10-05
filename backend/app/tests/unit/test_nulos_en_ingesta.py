"""Un ``U+0000`` no puede llegar a la base, y la marca de fórmula no puede tenerlo.

Incidente (ASTERIA, 2026-10-05): un ``.xlsx`` con 1.601 celdas de fórmula sin
calcular. La marca de esas celdas era ``"\\x00formula-sin-resultado\\x00"`` y
Postgres rechaza ``\\x00`` en JSONB: el worker no pudo guardar el resultado y el
archivo quedó en ``FAILED`` con el texto de asyncpg. SQLite lo guarda sin
quejarse, por eso estos tests miran el JSON serializado y no la base; el recorrido
contra Postgres real está en ``test_ingesta_nulos_pg.py``.
"""

from __future__ import annotations

import io
import json

import pytest
from openpyxl import Workbook

from app.application.services.file_parsing import parse_uploaded_content
from app.domain.ingestion_limits import (
    FORMULA_SIN_RESULTADO,
    FORMULA_SIN_RESULTADO_LEGADO,
    es_formula_sin_resultado,
)
from app.domain.texto_nulo import (
    NuloEnJsonError,
    buscar_nulo,
    desambiguar_encabezados,
)
from app.jobs.ingestion_worker import (
    ERROR_AL_GUARDAR,
    ERROR_PROCESAMIENTO,
    ERROR_RESULTADO_NO_PERSISTIBLE,
    resumen_de_fallo,
)
from app.persistence.db.engine_factory import serializar_json

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _sin_nulos(summary: dict[str, object]) -> None:
    """Lo que Postgres va a ver: el JSON serializado, sin ``\\u0000``."""
    assert buscar_nulo(summary) is None, f"nulo en {buscar_nulo(summary)}"
    serializar_json(summary)  # no levanta


# ── La marca de fórmula ──────────────────────────────────────────────────────


def test_la_marca_no_tiene_nulos() -> None:
    assert "\x00" not in FORMULA_SIN_RESULTADO


def test_se_reconocen_la_marca_nueva_y_la_vieja() -> None:
    """La vieja se sigue reconociendo: la identidad de la celda se decide antes
    de cualquier limpieza, así que quitar nulos nunca la convierte en texto."""
    assert es_formula_sin_resultado(FORMULA_SIN_RESULTADO)
    assert es_formula_sin_resultado(FORMULA_SIN_RESULTADO_LEGADO)
    assert not es_formula_sin_resultado("formula-sin-resultado")
    assert not es_formula_sin_resultado(None)


def test_un_libro_con_formulas_sin_calcular_produce_un_resumen_guardable() -> None:
    """El caso de ASTERIA, reducido: columnas calculadas que el archivo nunca
    calculó. Antes, cada una metía dos nulos en el resumen."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Precios"
    ws.append(["Producto", "Precio de compra", "Envio", "compra+envio"])
    for i in range(2, 6):
        # La última queda fuera del subconjunto que Véktor calcula (Fase 3), así
        # que su marca tiene que llegar al resumen.
        ws.append([f"Producto {i}", 1000 * i, f"=B{i}*0.1", f"=VLOOKUP(B{i},A1:B5,2)"])
    buffer = io.BytesIO()
    wb.save(buffer)

    summary = parse_uploaded_content(buffer.getvalue(), _XLSX, "precios.xlsx")

    assert summary["formulas_sin_resultado"], "la detección de fórmulas tiene que seguir"
    texto = json.dumps(summary)
    assert "formula-sin-resultado" in texto, "la marca tiene que sobrevivir al resumen"
    _sin_nulos(summary)
    # Y viaja ida y vuelta por JSON reconociéndose: es lo que lee el importador.
    vuelta = json.loads(texto)
    filas = [
        fila
        for clave in ("stock_detectado", "ventas_detectadas", "otros_detectados", "preview_rows")
        for fila in vuelta.get(clave, [])
        if isinstance(fila, dict)
    ]
    assert any(es_formula_sin_resultado(v) for fila in filas for v in fila.values())


# ── La limpieza de la entrada ────────────────────────────────────────────────


def test_un_csv_con_nulos_se_limpia_y_se_avisa() -> None:
    contenido = "fecha,detalle,monto\n2026-09-01,ve\x00la,1500\n2026-09-02,ok,200\n"
    summary = parse_uploaded_content(contenido.encode(), "text/csv", "ventas.csv")

    _sin_nulos(summary)
    assert summary["normalizaciones"]["celdas"] == 1
    assert summary["normalizaciones"]["nulos"] == 1
    ejemplo = summary["normalizaciones"]["ejemplos"][0]
    assert (ejemplo["fila"], ejemplo["columna"]) == (2, 2), "fila y columna como las ve el usuario"
    assert any("nulo" in w for w in summary["warnings"])
    assert any(f.get("detalle") == "vela" for f in summary["preview_rows"])


def test_solo_se_quita_el_nulo_no_otros_caracteres() -> None:
    """Limpiar "no imprimibles" en general cambiaría datos válidos."""
    contenido = "fecha,detalle,monto\n2026-09-01,a\tb\x01c\x00d,1500\n"
    summary = parse_uploaded_content(contenido.encode(), "text/csv", "ventas.csv")
    assert summary["preview_rows"][0]["detalle"] == "a\tb\x01cd"


def test_un_archivo_sin_nulos_no_registra_nada() -> None:
    contenido = "fecha,detalle,monto\n2026-09-01,vela,1500\n"
    summary = parse_uploaded_content(contenido.encode(), "text/csv", "ventas.csv")
    assert "normalizaciones" not in summary


def test_dos_encabezados_que_quedan_iguales_no_se_fusionan() -> None:
    """``Monto\\x00`` y ``Monto`` serían la misma clave y una columna se perdería."""
    contenido = "fecha,Monto,Mon\x00to\n2026-09-01,100,200\n"
    summary = parse_uploaded_content(contenido.encode(), "text/csv", "ventas.csv")

    _sin_nulos(summary)
    fila = summary["preview_rows"][0]
    assert fila["Monto"] == "100"
    assert fila["Monto (2)"] == "200"
    assert any("Monto (2)" in w for w in summary["warnings"])


def test_la_columna_con_nulos_se_renombra_aunque_venga_primero() -> None:
    """El orden del archivo no decide: antes, con la columna sucia ADELANTE, la
    limpia no se renombraba y quedaban dos «Monto» (una columna perdida)."""
    contenido = "fecha,Mon\x00to,Monto\n2026-09-01,200,100\n"
    summary = parse_uploaded_content(contenido.encode(), "text/csv", "ventas.csv")

    fila = summary["preview_rows"][0]
    assert fila["Monto"] == "100", "la columna que no tenía nulos conserva su nombre"
    assert fila["Monto (2)"] == "200"
    limpios, avisos = desambiguar_encabezados(["Monto\x00", "Monto"], ["Monto", "Monto"])
    assert limpios == ["Monto (2)", "Monto"]
    assert len(avisos) == 1


def test_desambiguar_solo_actua_sobre_colisiones_creadas_por_la_limpieza() -> None:
    """Dos encabezados que YA venían repetidos no son asunto de la limpieza."""
    limpios, avisos = desambiguar_encabezados(["a", "a", "b\x00"], ["a", "a", "b"])
    assert limpios == ["a", "a", "b"]
    assert avisos == []


def test_un_documento_con_nulos_se_limpia() -> None:
    texto = "Venta mostrador $ 1.500\x00\x00\nOtra venta $ 2.000\n"
    summary = parse_uploaded_content(texto.encode(), "text/plain", "notas.txt")
    _sin_nulos(summary)
    assert summary["normalizaciones"]["nulos"] == 2


# ── La red de salida ─────────────────────────────────────────────────────────


def test_el_serializador_rechaza_un_nulo_diciendo_donde_esta() -> None:
    with pytest.raises(NuloEnJsonError) as error:
        serializar_json({"mapping_contexts": [{"preview_rows": [{"monto": "1\x002"}]}]})
    assert error.value.ruta == "$.mapping_contexts[0].preview_rows[0].monto"


def test_el_serializador_rechaza_un_nulo_en_una_clave() -> None:
    with pytest.raises(NuloEnJsonError):
        serializar_json({"Mon\x00to": "1"})


def test_el_texto_literal_barra_u0000_no_es_un_nulo() -> None:
    """Seis caracteres que alguien escribió: es un dato, no un nulo."""
    assert json.loads(serializar_json({"nota": "\\u0000"})) == {"nota": "\\u0000"}


# ── Lo que ve el usuario cuando falla ────────────────────────────────────────


def test_un_nulo_al_guardar_se_explica_en_castellano() -> None:
    from sqlalchemy.exc import StatementError

    envuelto = StatementError("bind", "UPDATE ...", {}, NuloEnJsonError("$.x"))
    resumen = resumen_de_fallo(envuelto, "spreadsheet")
    assert resumen["error_code"] == ERROR_RESULTADO_NO_PERSISTIBLE
    assert "U+0000" not in resumen["error"] and "UPDATE" not in resumen["error"]


def test_un_error_de_base_no_muestra_el_texto_del_driver() -> None:
    from sqlalchemy.exc import DBAPIError

    error = DBAPIError("UPDATE uploaded_files SET ...", {}, Exception("asyncpg boom"))
    resumen = resumen_de_fallo(error, "text")
    assert resumen["error_code"] == ERROR_AL_GUARDAR
    assert "asyncpg" not in resumen["error"] and "UPDATE" not in resumen["error"]
    assert resumen["file_type"] == "text"


def test_un_error_del_parseo_se_conserva() -> None:
    """Los errores propios del parseo ya se escriben para el usuario."""
    resumen = resumen_de_fallo(ValueError("hoja ilegible"), "spreadsheet")
    assert resumen == {
        "error": "hoja ilegible",
        "error_code": ERROR_PROCESAMIENTO,
        "file_type": "spreadsheet",
    }
