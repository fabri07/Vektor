"""Tests unitarios de las funciones puras de scripts/detect_misvoided_purchases.py.

Ninguna depende de DB: ``_score_candidate`` opera sobre dicts en memoria (voideada +
socios de cluster + set de product_ids con divergencia negativa); ``_build_repair_audit_map``
y ``_negative_divergence_product_ids`` operan sobre filas ya traídas (mappings) /
decision_data ya deserializado. Se carga el módulo por ruta de archivo (``scripts/``
no es un paquete) — mismo patrón que ``test_reconcile_untagged_adjustments.py``.
Importar el módulo dispara ``from _db import async_engine_config`` a nivel módulo,
pero ``_db`` solo define helpers (no conecta), así que el import es seguro sin
``DATABASE_URL``.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module():
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "detect_misvoided_purchases", _SCRIPTS_DIR / "detect_misvoided_purchases.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


_UPLOAD_A = str(uuid.uuid4())
_UPLOAD_B = str(uuid.uuid4())
_HASH_A = "hash-a"
_HASH_B = "hash-b"


def _voided(source_upload_id=None, source_row_hash=None, product_id="prod-1") -> dict:
    return {
        "product_id": product_id,
        "source_upload_id": source_upload_id,
        "source_row_hash": source_row_hash,
    }


def _partner(mid=None, source_upload_id=None, source_row_hash=None) -> dict:
    return {
        "id": mid or uuid.uuid4(),
        "source_upload_id": source_upload_id,
        "source_row_hash": source_row_hash,
    }


# --------------------------------------------------------------------- _score_candidate


def test_distinct_uploads_true_when_both_present_and_different(mod):
    voided = _voided(source_upload_id=_UPLOAD_A)
    partners = [_partner(source_upload_id=_UPLOAD_B)]
    result = mod._score_candidate(voided, partners, set())
    assert result["distinct_uploads"] is True
    assert result["misvoid_score"] == 1


def test_distinct_uploads_false_when_same_upload(mod):
    voided = _voided(source_upload_id=_UPLOAD_A)
    partners = [_partner(source_upload_id=_UPLOAD_A)]
    result = mod._score_candidate(voided, partners, set())
    assert result["distinct_uploads"] is False
    assert result["misvoid_score"] == 0


def test_distinct_uploads_none_safe_when_voided_upload_missing(mod):
    """voided.source_upload_id None -> nunca True, aunque el socio tenga upload."""
    voided = _voided(source_upload_id=None)
    partners = [_partner(source_upload_id=_UPLOAD_A)]
    result = mod._score_candidate(voided, partners, set())
    assert result["distinct_uploads"] is False


def test_distinct_uploads_none_safe_when_partner_upload_missing(mod):
    """partner.source_upload_id None -> no cuenta como "distinto"."""
    voided = _voided(source_upload_id=_UPLOAD_A)
    partners = [_partner(source_upload_id=None)]
    result = mod._score_candidate(voided, partners, set())
    assert result["distinct_uploads"] is False


def test_distinct_hashes_true_when_both_present_and_different(mod):
    voided = _voided(source_row_hash=_HASH_A)
    partners = [_partner(source_row_hash=_HASH_B)]
    result = mod._score_candidate(voided, partners, set())
    assert result["distinct_hashes"] is True
    assert result["misvoid_score"] == 1


def test_distinct_hashes_false_when_both_none(mod):
    voided = _voided(source_row_hash=None)
    partners = [_partner(source_row_hash=None)]
    result = mod._score_candidate(voided, partners, set())
    assert result["distinct_hashes"] is False


def test_integrity_divergence_negative_true_when_product_in_set(mod):
    voided = _voided(product_id="prod-42")
    result = mod._score_candidate(voided, [], {"prod-42"})
    assert result["integrity_divergence_negative"] is True
    assert result["misvoid_score"] == 1


def test_integrity_divergence_negative_false_when_product_not_in_set(mod):
    voided = _voided(product_id="prod-42")
    result = mod._score_candidate(voided, [], {"other-product"})
    assert result["integrity_divergence_negative"] is False


def test_score_aggregates_all_three_signals(mod):
    voided = _voided(source_upload_id=_UPLOAD_A, source_row_hash=_HASH_A, product_id="prod-42")
    partners = [_partner(source_upload_id=_UPLOAD_B, source_row_hash=_HASH_B)]
    result = mod._score_candidate(voided, partners, {"prod-42"})
    assert result["distinct_uploads"] is True
    assert result["distinct_hashes"] is True
    assert result["integrity_divergence_negative"] is True
    assert result["misvoid_score"] == 3


def test_no_partner_computes_divergence_signal_but_not_partner_dependent_ones(mod):
    """Sin socio de cluster: las señales partner-dependientes (uploads/hashes) quedan
    en False (sin evidencia, no True espurio), pero la señal de divergencia -que no
    depende de partners- se computa igual."""
    voided = _voided(source_upload_id=_UPLOAD_A, source_row_hash=_HASH_A, product_id="prod-42")
    result = mod._score_candidate(voided, [], {"prod-42"})
    assert result["partner_movement_ids"] == []
    assert result["distinct_uploads"] is False
    assert result["distinct_hashes"] is False
    assert result["integrity_divergence_negative"] is True
    assert result["misvoid_score"] == 1


def test_partner_movement_ids_lists_all_partners(mod):
    p1, p2 = _partner(), _partner()
    voided = _voided()
    result = mod._score_candidate(voided, [p1, p2], set())
    assert set(result["partner_movement_ids"]) == {str(p1["id"]), str(p2["id"])}


# --------------------------------------------------------------- _negative_divergence_product_ids


def test_negative_divergence_extracts_only_diff_below_zero(mod):
    decision_data = {
        "divergences": [
            {"product_id": "p1", "diff": -5},
            {"product_id": "p2", "diff": 3},
            {"product_id": "p3", "diff": 0},
        ]
    }
    assert mod._negative_divergence_product_ids(decision_data) == {"p1"}


def test_negative_divergence_none_safe_missing_diff(mod):
    decision_data = {"divergences": [{"product_id": "p1"}]}
    assert mod._negative_divergence_product_ids(decision_data) == set()


def test_negative_divergence_none_safe_no_decision_data(mod):
    assert mod._negative_divergence_product_ids(None) == set()


def test_negative_divergence_accepts_json_string(mod):
    import json

    decision_data = json.dumps({"divergences": [{"product_id": "p9", "diff": -1}]})
    assert mod._negative_divergence_product_ids(decision_data) == {"p9"}


# --------------------------------------------------------------------- _build_repair_audit_map


def test_repair_audit_map_structured_extraction(mod):
    mv_id = str(uuid.uuid4())
    audit_id = uuid.uuid4()
    audit_rows = [{"id": audit_id, "decision_data": {"step": "B1", "voided_movement_ids": [mv_id]}}]
    mapping = mod._build_repair_audit_map({mv_id}, audit_rows)
    assert mapping == {mv_id: str(audit_id)}


def test_repair_audit_map_fallback_substring_match(mod):
    """Sin la clave estructurada 'voided_movement_ids' -> cae al substring match
    (mismo truco que diag_missing_purchases_scope.py)."""
    mv_id = str(uuid.uuid4())
    audit_id = uuid.uuid4()
    audit_rows = [
        {"id": audit_id, "decision_data": {"reason": "some_other_shape", "ids_touched": [mv_id]}}
    ]
    mapping = mod._build_repair_audit_map({mv_id}, audit_rows)
    assert mapping == {mv_id: str(audit_id)}


def test_repair_audit_map_unmatched_id_absent(mod):
    mv_id = str(uuid.uuid4())
    audit_rows = [{"id": uuid.uuid4(), "decision_data": {"voided_movement_ids": []}}]
    mapping = mod._build_repair_audit_map({mv_id}, audit_rows)
    assert mapping == {}
