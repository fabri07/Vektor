"""Reporte de ``scripts/diag_ediciones_en_riesgo.py`` (B·nuevo paso 0a).

Las consultas son Postgres-only (``jsonb_typeof``, ``?``) y se verificaron contra
``vektor_pgtest``; acá se prueba la pieza pura que arma el reporte: ausencia de
dato nunca sale como cero, tampoco cuando no hay ningún tenant con datos.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from diag_ediciones_en_riesgo import (  # noqa: E402
    _SIN_CONTEXTO,
    _SIN_TRAZA_DE_FRENOS,
    informe,
)

BASE = "host=localhost base=vektor_pgtest"


def test_sin_tenants_dice_la_base_e_imprime_sin_medicion() -> None:
    texto = "\n".join(informe(BASE, None, {}, {}, {}))
    assert texto.startswith(f"Base leída: {BASE}")
    assert "ni relecturas frenadas" not in texto
    assert _SIN_TRAZA_DE_FRENOS in texto
    assert _SIN_CONTEXTO in texto
    # Lo que la base SÍ guarda es una cuenta hecha: 0.
    assert "registros editados a mano de archivos importados" in texto


def test_con_y_sin_filtro_imprimen_las_mismas_lineas_sin_medicion() -> None:
    sin_filtro = informe(BASE, None, {}, {}, {})
    con_filtro = informe(BASE, "00000000-0000-0000-0000-000000000001", {}, {}, {})
    sin_med = [ln for ln in sin_filtro if "sin medición (" in ln]
    assert len(sin_med) == 2
    assert sin_med == [ln for ln in con_filtro if "sin medición (" in ln]
