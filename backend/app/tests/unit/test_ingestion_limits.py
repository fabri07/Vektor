"""E6c-2 — hasta dónde Véktor lee un archivo, y qué dice cuando no puede.

Antes de esto el único tope era el tamaño del upload: 16 MB. Un archivo de 16 MB
con una hoja de un millón de filas se intentaba entero, y lo que pasaba no era un
error sino un proceso consumiendo memoria hasta que alguien lo mataba, sin dejar
dicho por qué.

Los tres criterios que gobiernan estos tests:

* **Justo debajo pasa, en el tope pasa, por encima se rechaza.** Un límite que no
  se prueba en su borde no se sabe si está en el número que dice.
* **Nunca se importa una parte.** Truncar produce libros que no cuadran y que
  nadie sabe que están incompletos: es peor que rechazar.
* **El motivo es específico.** "El archivo es demasiado grande" obliga a adivinar
  cuál de las siete dimensiones era.
"""

from __future__ import annotations

import io
import time
import zipfile
from typing import Any

import pytest
from openpyxl import Workbook

from app.application.services.file_parsing import (
    _leer_hoja_acotada,
    parse_uploaded_content,
    verificar_tamano_expandido,
)
from app.domain.ingestion_limits import (
    LIMITE_ARCHIVO_DANADO,
    LIMITE_BYTES,
    LIMITE_CELDAS,
    LIMITE_COLUMNAS,
    LIMITE_EXPANDIDO,
    LIMITE_FILAS,
    LIMITE_HOJAS,
    LIMITE_TIEMPO,
    LimiteExcedidoError,
    LimitesDeArchivo,
    RelojDeParsing,
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _libro(*, filas: int = 3, columnas: int = 3, hojas: int = 1) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for h in range(hojas):
        ws = wb.create_sheet(f"Hoja{h}")
        ws.append([f"col{c}" for c in range(columnas)])
        for f in range(filas):
            ws.append([f"v{f}_{c}" for c in range(columnas)])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ── Bordes: debajo, en el tope, por encima ───────────────────────────────────
@pytest.mark.parametrize(
    ("filas_archivo", "tope", "rechaza"),
    [(4, 5, False), (5, 5, False), (6, 5, True)],
)
def test_el_tope_de_filas_esta_donde_dice(
    filas_archivo: int, tope: int, rechaza: bool
) -> None:
    """Debajo pasa, en el tope pasa, por encima rechaza.

    El caso "en el tope" es el que más importa: un `>` escrito como `>=` rechaza
    archivos legítimos, y nadie lo nota hasta que un cliente no puede importar.
    """
    # +1 por el encabezado, que también es una fila del archivo.
    contenido = _libro(filas=filas_archivo - 1)
    limites = LimitesDeArchivo(filas=tope)
    if rechaza:
        with pytest.raises(LimiteExcedidoError) as exc:
            parse_uploaded_content(contenido, _XLSX, "x.xlsx", limites)
        assert exc.value.limite == LIMITE_FILAS
    else:
        assert parse_uploaded_content(contenido, _XLSX, "x.xlsx", limites)


def test_el_tope_de_columnas_nombra_las_columnas() -> None:
    with pytest.raises(LimiteExcedidoError) as exc:
        parse_uploaded_content(
            _libro(columnas=10), _XLSX, "x.xlsx", LimitesDeArchivo(columnas=5)
        )
    assert exc.value.limite == LIMITE_COLUMNAS
    assert "columnas" in exc.value.mensaje


def test_el_tope_de_hojas_nombra_las_hojas() -> None:
    with pytest.raises(LimiteExcedidoError) as exc:
        parse_uploaded_content(
            _libro(hojas=6), _XLSX, "x.xlsx", LimitesDeArchivo(hojas=3)
        )
    assert exc.value.limite == LIMITE_HOJAS
    assert "hojas" in exc.value.mensaje


def test_las_celdas_se_acumulan_entre_hojas() -> None:
    """Seis hojas de 16 celdas no pasan ningún tope individual y son 96.

    Sin el acumulado, un libro con muchas hojas chicas atraviesa los topes de
    filas y de columnas sin despeinarse.
    """
    contenido = _libro(filas=3, columnas=4, hojas=6)
    generoso = LimitesDeArchivo(filas=1_000, columnas=1_000, hojas=100, celdas=50)
    with pytest.raises(LimiteExcedidoError) as exc:
        parse_uploaded_content(contenido, _XLSX, "x.xlsx", generoso)
    assert exc.value.limite == LIMITE_CELDAS


def test_el_tope_de_bytes_se_verifica_en_el_parser_no_solo_en_el_endpoint() -> None:
    """Los tres caminos del worker leen de S3 y nunca pasan por el chequeo HTTP.

    Un tope que viviera sólo en el endpoint no protegería justo al proceso que
    corre sin nadie mirando.
    """
    with pytest.raises(LimiteExcedidoError) as exc:
        parse_uploaded_content(
            b"x" * 1_000, _XLSX, "x.xlsx", LimitesDeArchivo(bytes_comprimido=100)
        )
    assert exc.value.limite == LIMITE_BYTES


def test_cada_motivo_dice_que_hacer() -> None:
    """Un mensaje que sólo dice "demasiado grande" obliga a adivinar cuál de las
    siete dimensiones era y qué hacer al respecto."""
    for limite in (
        LIMITE_BYTES,
        LIMITE_EXPANDIDO,
        LIMITE_FILAS,
        LIMITE_COLUMNAS,
        LIMITE_HOJAS,
        LIMITE_CELDAS,
        LIMITE_TIEMPO,
    ):
        mensaje = LimiteExcedidoError(limite, 1_000_000, 100).mensaje
        assert len(mensaje) > 60, f"{limite}: el mensaje no explica nada"
        # Todos proponen una acción concreta.
        assert any(
            verbo in mensaje.lower()
            for verbo in ("dividí", "dividila", "dividilo", "borra", "dejá", "probá", "revisá")
        ), f"{limite}: el mensaje no dice qué hacer — «{mensaje}»"


# ── Tamaño expandido: no confiar en los metadatos del ZIP ────────────────────
def test_el_expandido_se_cuenta_descomprimiendo_no_leyendo_el_encabezado() -> None:
    """Un ZIP puede DECLARAR un tamaño y expandir otro.

    Los metadatos los escribe quien arma el archivo. Si el control se quedara en
    lo declarado, un archivo que miente hacia abajo pasaría el tope y expandiría
    lo que quisiera — que es exactamente la forma de un zip bomb.
    """
    payload = b"A" * (2 * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", payload)
    contenido = buffer.getvalue()

    # Comprime muchísimo: el archivo es chico y expande 2 MB.
    assert len(contenido) < 100 * 1024

    with pytest.raises(LimiteExcedidoError) as exc:
        verificar_tamano_expandido(contenido, LimitesDeArchivo(bytes_expandido=1024))
    assert exc.value.limite == LIMITE_EXPANDIDO


def test_el_expandido_corta_antes_de_terminar_de_descomprimir() -> None:
    """El contador aborta al cruzar el tope, no después de materializar todo.

    Si midiera descomprimiendo entero y comparando al final, el tope no acotaría
    nada: el daño (la memoria) ya estaría hecho cuando se detecta.
    """
    payload = b"B" * (8 * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("grande.xml", payload)

    t0 = time.perf_counter()
    with pytest.raises(LimiteExcedidoError):
        verificar_tamano_expandido(
            buffer.getvalue(), LimitesDeArchivo(bytes_expandido=64 * 1024)
        )
    # El valor reportado es el del corte, no el total del archivo: prueba que se
    # dejó de leer. Con 64 KB de tope, no puede haber contado 8 MB.
    assert time.perf_counter() - t0 < 5.0


def test_un_archivo_que_no_es_zip_no_tiene_expansion_que_medir() -> None:
    assert verificar_tamano_expandido(b"fecha,monto\n2024-01-01,100\n") == 27


def test_un_xlsx_normal_pasa_el_control_de_expandido() -> None:
    """La compuerta no puede rechazar el caso normal: un libro chico expande
    poco y tiene que pasar sin que nadie lo note."""
    contenido = _libro(filas=50, columnas=8)
    assert verificar_tamano_expandido(contenido) > 0
    assert parse_uploaded_content(contenido, _XLSX, "x.xlsx")


# ── Tiempo: detener el trabajo, no sólo devolver error ───────────────────────
def test_el_reloj_corta_el_trabajo_y_no_solo_avisa() -> None:
    """Un timeout que devuelve error mientras el proceso sigue leyendo no acota
    ningún recurso: el archivo se termina de procesar igual, sólo que nadie mira
    el resultado. Acá se afirma que el BUCLE se corta.
    """
    reloj = RelojDeParsing(LimitesDeArchivo(segundos=0.01))
    time.sleep(0.05)
    vueltas = 0
    with pytest.raises(LimiteExcedidoError) as exc:
        for _ in range(10_000_000):
            vueltas += 1
            reloj.controlar()
    assert exc.value.limite == LIMITE_TIEMPO
    # Cortó cerca del principio: si el reloj sólo avisara al final, `vueltas`
    # habría llegado a los diez millones.
    assert vueltas < 5_000, f"el bucle siguió {vueltas} vueltas después del tope"


def test_el_reloj_no_molesta_cuando_hay_tiempo() -> None:
    reloj = RelojDeParsing(LimitesDeArchivo(segundos=60))
    for _ in range(5_000):
        reloj.controlar()  # no levanta


def test_el_parsing_de_un_archivo_normal_no_se_corta_por_tiempo() -> None:
    """La compuerta del tiempo tampoco puede tocar el caso normal."""
    assert parse_uploaded_content(_libro(filas=100), _XLSX, "x.xlsx")


# ── Nunca una parte ──────────────────────────────────────────────────────────
def test_el_rechazo_no_devuelve_un_summary_parcial(monkeypatch: Any) -> None:
    """Rechazar es levantar, no devolver lo que se alcanzó a leer.

    Si el parser devolviera un summary con las primeras N filas, el import
    seguiría adelante y produciría libros incompletos que nadie sabe que lo son.
    """
    with pytest.raises(LimiteExcedidoError):
        parse_uploaded_content(
            _libro(filas=100), _XLSX, "x.xlsx", LimitesDeArchivo(filas=10)
        )


# ── Las dos pruebas que el mutation testing exigió ──────────────────────────
def _zip_que_miente_sobre_su_tamano(payload: bytes) -> bytes:
    """Un ZIP cuyos encabezados DECLARAN 1 byte y que expande ``payload``.

    Hace falta forjarlo a mano: ``zipfile`` recalcula el tamaño al escribir, así
    que un ZIP armado con la librería nunca miente y un test que use uno no puede
    distinguir "leemos el encabezado" de "descomprimimos y contamos". El campo de
    tamaño sin comprimir aparece dos veces —en el encabezado local (offset 22) y
    en el directorio central (offset 24)— y hay que falsear los dos.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", payload)
    crudo = bytearray(buffer.getvalue())

    mentira = (1).to_bytes(4, "little")
    verdadero = len(payload).to_bytes(4, "little")
    # Encabezado local: firma PK\x03\x04, tamaño sin comprimir en +22.
    local = crudo.find(b"PK\x03\x04")
    assert crudo[local + 22 : local + 26] == verdadero
    crudo[local + 22 : local + 26] = mentira
    # Directorio central: firma PK\x01\x02, tamaño sin comprimir en +24.
    central = crudo.find(b"PK\x01\x02")
    assert crudo[central + 24 : central + 28] == verdadero
    crudo[central + 24 : central + 28] = mentira
    return bytes(crudo)


def test_un_zip_que_miente_sobre_su_tamano_se_rechaza_diciendolo() -> None:
    """Un archivo cuyos encabezados no coinciden con su contenido no entra.

    Este test nació buscando otra cosa —probar que no confiamos en los metadatos—
    y encontró algo mejor: ``zipfile`` corta la lectura en el tamaño DECLARADO y
    levanta ``BadZipFile`` cuando el CRC no da. O sea que un ZIP que miente hacia
    abajo no logra expandir de más; lo que lograba era salir como error genérico
    ("algo salió mal"), sin decirle al usuario que el problema es su archivo.

    Ahora se rechaza con causa: dañado o modificado por fuera del programa que lo
    generó, y qué hacer (volver a exportarlo).

    Lo que el contador de bytes sí aporta, y por eso sigue: **corta durante** la
    descompresión en vez de después, así que acota el trabajo pico y no depende de
    que ``infolist()`` sea completo.
    """
    contenido = _zip_que_miente_sobre_su_tamano(b"C" * (4 * 1024 * 1024))
    with pytest.raises(LimiteExcedidoError) as exc:
        verificar_tamano_expandido(contenido, LimitesDeArchivo(bytes_expandido=64 * 1024))
    assert exc.value.limite == LIMITE_ARCHIVO_DANADO
    assert "dañado" in exc.value.mensaje


class _HojaEspia:
    """Una hoja que cuenta cuántas filas le pidieron. Perezosa, como openpyxl."""

    def __init__(self, filas: int) -> None:
        self.total = filas
        self.leidas = 0

    def iter_rows(self, values_only: bool = True) -> Any:  # noqa: ARG002, FBT001, FBT002
        for i in range(self.total):
            self.leidas += 1
            yield (f"a{i}", f"b{i}")


def test_el_tope_de_tiempo_deja_de_leer_filas() -> None:
    """No alcanza con devolver error: hay que DEJAR DE TRABAJAR.

    Un timeout que levanta después de haber leído el archivo entero no acota
    ningún recurso — la memoria y el CPU ya se gastaron, sólo que nadie mira el
    resultado. Acá se cuenta cuántas filas se pidieron: si el control estuviera
    después del bucle, serían las 100.000.

    Esta prueba la exigió el mutation testing: mover `reloj.controlar()` fuera del
    bucle no rompía ningún test, porque el anterior ejercitaba el reloj suelto y
    no el parser.
    """
    from app.application.services.file_parsing import _leer_hoja_acotada

    hoja = _HojaEspia(100_000)
    limites = LimitesDeArchivo(segundos=0.0)
    reloj = RelojDeParsing(limites)
    with pytest.raises(LimiteExcedidoError) as exc:
        _leer_hoja_acotada(hoja, reloj, limites, 0)
    assert exc.value.limite == LIMITE_TIEMPO
    assert hoja.leidas < 1_000, (
        f"se leyeron {hoja.leidas} de {hoja.total} filas después de pasarse del "
        "tope: el control no está deteniendo el trabajo"
    )


def test_un_archivo_chico_tambien_consulta_el_reloj() -> None:
    """El control se hace en la primera llamada y después cada tanto.

    Sin el caso especial de la primera, un archivo de menos de 512 filas no
    consultaba el reloj **ni una vez** y el tope no existía para él. No es
    teórico: 500 filas con miles de fórmulas tardan como 50.000 simples.
    """
    hoja = _HojaEspia(10)
    limites = LimitesDeArchivo(segundos=0.0)
    with pytest.raises(LimiteExcedidoError):
        _leer_hoja_acotada(hoja, RelojDeParsing(limites), limites, 0)
