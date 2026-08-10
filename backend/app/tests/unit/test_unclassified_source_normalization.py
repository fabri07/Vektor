"""La procedencia de una fila capturada tiene que caber en la CHECK de su columna.

`unclassified_records.source` es un set cerrado (`ingestion`, `chat`,
`reanalysis`), pero el importador recibe la procedencia como string libre y la
relectura se nombra a sí misma ``"reread"``. Capturar una fila durante una
relectura levantaba `ck_unclassified_records_source` y —al no atraparse en ningún
lado— abortaba la transacción entera del apply: una operación que ya había
reimportado ventas se caía por el registro de una fila que no se pudo clasificar.

Estuvo latente desde que existe la relectura (cualquier captura la disparaba:
fecha ilegible, producto ambiguo, riesgo de columna). F-H4 la volvió común, porque
toda fila sin monto pasó a capturarse.
"""

from __future__ import annotations

import pytest

from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_SOURCES,
    normalize_unclassified_source,
)


@pytest.mark.parametrize("source", UNCLASSIFIED_SOURCES)
def test_las_procedencias_validas_pasan_intactas(source: str) -> None:
    assert normalize_unclassified_source(source) == source


def test_la_relectura_se_guarda_como_reanalisis() -> None:
    """Una relectura ES un reanálisis del mismo archivo: no se inventa una
    procedencia nueva ni se pierde la fila."""
    assert normalize_unclassified_source("reread") == "reanalysis"


def test_una_procedencia_desconocida_no_rompe_el_import() -> None:
    """La fila capturada es el ÚNICO rastro de un dato que no se pudo importar.
    Perderla —y encima abortar una operación que ya escribió— es peor que
    guardarla con una procedencia genérica."""
    assert normalize_unclassified_source("cualquier_cosa") in UNCLASSIFIED_SOURCES


def test_el_resultado_siempre_satisface_la_check() -> None:
    for entrada in ("ingestion", "chat", "reanalysis", "reread", "", "batch_auto"):
        assert normalize_unclassified_source(entrada) in UNCLASSIFIED_SOURCES
