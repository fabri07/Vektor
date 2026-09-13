"""Cambio 4 — comprobante de importación (domain, puro)."""

from __future__ import annotations

from app.domain.import_receipt import build_import_receipt

_CTX = [
    {
        "context_id": "sheet:Catalogo",
        "label": "Catálogo",
        "headers": ["nombre", "tienda", "precio", "sin_tocar", "notas"],
        "row_count": 10,
    }
]

_MAPPING = {
    "sheet:Catalogo": {
        "nombre": "name",
        "tienda": "marca",
        "precio": "sale_price_ars",
        "notas": "ignore",
    }
}


def _by_col(entries, col):
    return next(e for e in entries if e.source_column == col)


def test_columna_sin_mapear_no_desaparece():
    entries = build_import_receipt(contexts=_CTX, effective_mapping=_MAPPING)
    e = _by_col(entries, "sin_tocar")
    assert e.result == "excluido"
    assert e.reason_kind == "system_rule"
    assert e.target_field is None


def test_columna_ignorada_es_decision_del_usuario():
    entries = build_import_receipt(contexts=_CTX, effective_mapping=_MAPPING)
    e = _by_col(entries, "notas")
    assert e.result == "excluido"
    assert e.reason_kind == "user_decision"


def test_precio_mapeado_a_campo_numerico_es_transformado():
    entries = build_import_receipt(contexts=_CTX, effective_mapping=_MAPPING)
    e = _by_col(entries, "precio")
    assert e.result == "transformado"
    assert e.rows_affected is None


def test_columna_dropeada_por_riesgo_es_excluida_por_decision_del_usuario():
    entries = build_import_receipt(
        contexts=_CTX,
        effective_mapping=_MAPPING,
        dropped_columns={"sheet:Catalogo": ["tienda"]},
    )
    e = _by_col(entries, "tienda")
    assert e.result == "excluido"
    assert e.reason_kind == "user_decision"
    assert "riesgo" in (e.reason or "")


def test_columna_con_todas_las_filas_ruteadas_es_pendiente():
    entries = build_import_receipt(
        contexts=_CTX,
        effective_mapping=_MAPPING,
        routed_rows={"sheet:Catalogo": {i: {"precio": "x"} for i in range(10)}},
    )
    e = _by_col(entries, "precio")
    assert e.result == "pendiente"
    assert e.rows_affected == 10


def test_columna_con_algunas_filas_ruteadas_es_mixta_y_acredita_el_conteo_real():
    entries = build_import_receipt(
        contexts=_CTX,
        effective_mapping=_MAPPING,
        routed_rows={"sheet:Catalogo": {0: {"precio": "x"}, 1: {"precio": "x"}}},
    )
    e = _by_col(entries, "precio")
    assert e.result == "mixto"
    assert e.rows_affected == 2


def test_conflicto_de_external_code_se_acredita_si_una_sola_hoja_lo_mapea():
    mapping = {"sheet:Catalogo": {**_MAPPING["sheet:Catalogo"], "sin_tocar": "external_code"}}
    entries = build_import_receipt(
        contexts=_CTX, effective_mapping=mapping, external_code_conflicts=3
    )
    e = _by_col(entries, "sin_tocar")
    assert e.result == "mixto"
    assert e.rows_affected == 3
    assert e.reason_kind == "system_rule"


def test_conflicto_de_external_code_no_se_reparte_entre_dos_hojas():
    """Si dos hojas mapean a external_code, el contador global no se puede
    atribuir a ninguna con certeza — se declara sin desglose, no se inventa."""
    ctx_dos_hojas = [
        {**_CTX[0], "context_id": "a", "headers": ["codigo"]},
        {"context_id": "b", "label": "B", "headers": ["codigo"], "row_count": 5},
    ]
    mapping = {
        "a": {"codigo": "external_code"},
        "b": {"codigo": "external_code"},
    }
    entries = build_import_receipt(
        contexts=ctx_dos_hojas, effective_mapping=mapping, external_code_conflicts=2
    )
    for e in entries:
        assert e.result == "mixto"
        assert e.rows_affected is None


def test_columna_mapeada_sin_incidentes_es_guardada():
    entries = build_import_receipt(contexts=_CTX, effective_mapping=_MAPPING)
    e = _by_col(entries, "nombre")
    assert e.result == "guardado"
    assert e.reason is None
    assert e.rows_affected is None


def test_as_dict_serializa_todos_los_campos():
    entries = build_import_receipt(contexts=_CTX, effective_mapping=_MAPPING)
    d = entries[0].as_dict()
    assert set(d) == {
        "context_id", "context_label", "source_column", "target_field",
        "result", "reason", "reason_kind", "rows_affected",
    }
