"""F-T — el medidor por etapas del confirm.

Lo que se prueba acá no es "cuánto tarda" (eso depende de la máquina) sino que el
medidor **no mienta**: que sume las repeticiones en vez de quedarse con la última,
que un bloque que explota deje su tiempo registrado antes de propagar, y que el
total no salga de sumar etapas anidadas.
"""

from __future__ import annotations

import time

import pytest

from app.domain.stage_timing import StageTimings


def test_una_etapa_registra_ms_y_una_llamada() -> None:
    t = StageTimings()
    with t.stage("import"):
        pass

    detalle = t.as_detail()
    assert detalle["stages"]["import"]["calls"] == 1
    assert detalle["stages"]["import"]["ms"] >= 0


def test_la_misma_etapa_repetida_suma_en_vez_de_pisar() -> None:
    """Una etapa por hoja se llama N veces: quedarse con la última mediría una hoja.

    Es el modo de falla que vuelve inútil al medidor justo en el archivo grande,
    que es el único donde importa.
    """
    t = StageTimings()
    for _ in range(9):
        with t.stage("insercion_por_hoja") as etapa:
            etapa.rows = 100

    detalle = t.as_detail()["stages"]["insercion_por_hoja"]
    assert detalle["calls"] == 9
    assert detalle["rows"] == 900


def test_los_milisegundos_tambien_suman_no_solo_las_llamadas() -> None:
    """Contar llamadas no alcanza: con `acc.ms = elapsed` (pisar en vez de sumar)
    el contador de llamadas sigue dando 9 y el tiempo reportado es el de UNA hoja.

    Dos bloques de 20 ms no pueden dar menos de 30 salvo que se esté pisando; una
    máquina cargada los hace más largos, nunca más cortos.
    """
    t = StageTimings()
    for _ in range(2):
        with t.stage("lenta"):
            time.sleep(0.02)

    assert t.as_detail()["stages"]["lenta"]["ms"] >= 30


def test_declarar_cero_filas_no_es_lo_mismo_que_no_declararlas() -> None:
    """Una hoja incluida que no importó ninguna fila es un hallazgo; con un
    `if handle.rows:` en vez de `is not None` desaparecería de la traza justo
    cuando es interesante."""
    t = StageTimings()
    with t.stage("hoja_vacia") as etapa:
        etapa.rows = 0

    assert t.as_detail()["stages"]["hoja_vacia"]["rows"] == 0


def test_las_filas_se_pueden_declarar_despues_de_correr_el_bloque() -> None:
    """El importador devuelve sus conteos al final: exigir `rows` de antemano
    obligaría a adivinarlos o a no registrarlos."""
    t = StageTimings()
    with t.stage("import") as etapa:
        etapa.rows = 1187

    assert t.as_detail()["stages"]["import"]["rows"] == 1187


def test_una_etapa_sin_filas_no_inventa_un_cero() -> None:
    """`rows: 0` y "esta etapa no cuenta filas" son cosas distintas. Un cero acá
    haría leer «el finalize no procesó ninguna fila» como un dato de negocio."""
    t = StageTimings()
    with t.stage("finalize"):
        pass

    assert "rows" not in t.as_detail()["stages"]["finalize"]


def test_un_bloque_que_explota_registra_su_tiempo_y_propaga() -> None:
    """Un confirm que falla tiene que decir DÓNDE tardó antes de morir: es el
    caso en que la traza más se necesita."""
    t = StageTimings()

    with pytest.raises(ValueError, match="boom"), t.stage("import"):
        raise ValueError("boom")

    assert t.as_detail()["stages"]["import"]["calls"] == 1


def test_mark_mide_desde_el_arranque_y_despues_desde_el_mark_anterior() -> None:
    """`mark` existe porque envolver 800 líneas del confirm en un `with` obligaría
    a re-indentarlas: el diff taparía el cambio real."""
    t = StageTimings()
    time.sleep(0.02)
    t.mark("validaciones")
    t.mark("lease")

    stages = t.as_detail()["stages"]
    assert stages["validaciones"]["ms"] >= 15
    # El segundo mark arranca donde terminó el primero, no en el origen.
    assert stages["lease"]["ms"] < stages["validaciones"]["ms"]


def test_una_etapa_medida_con_stage_no_se_cuenta_otra_vez_en_el_mark_siguiente() -> None:
    """Si `stage` no moviera el cursor, el `mark` posterior volvería a cobrar el
    bloque que `stage` ya midió, y el archivo grande aparecería tardando el doble."""
    t = StageTimings()
    with t.stage("import"):
        time.sleep(0.03)
    t.mark("finalize")

    stages = t.as_detail()["stages"]
    assert stages["import"]["ms"] >= 25
    assert stages["finalize"]["ms"] < 20


def test_el_total_no_es_la_suma_de_las_etapas() -> None:
    """Con etapas anidadas, sumar contaría dos veces la interna. El total es
    tiempo de pared desde que arrancó la medición."""
    t = StageTimings()
    with t.stage("externa"), t.stage("interna"):
        time.sleep(0.01)

    detalle = t.as_detail()
    suma = sum(e["ms"] for e in detalle["stages"].values())
    assert detalle["total_ms"] <= suma
    assert detalle["total_ms"] >= detalle["stages"]["interna"]["ms"]


def test_sin_ninguna_etapa_sigue_dando_un_detalle_valido() -> None:
    """Si el confirm rebota en la primera validación no hay etapas cerradas, y el
    evento igual tiene que poder escribirse."""
    detalle = StageTimings().as_detail()
    assert detalle["stages"] == {}
    assert detalle["total_ms"] >= 0
