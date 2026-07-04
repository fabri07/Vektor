"""Tests unitarios de ``_classify_adjustment`` en scripts/reconcile_untagged_adjustments.py.

No dependen de DB: ``_classify_adjustment`` es una función pura sobre un dict de
movimiento + el índice de uploads + el set de movement_ids auditados. Se carga el
módulo por ruta de archivo (``scripts/`` no es un paquete) — mismo patrón que
``test_repair_inventory_ledger_classify.py``. Importar el módulo dispara
``from _db import async_engine_config`` a nivel módulo, pero ``_db`` solo define
helpers (no conecta), así que el import es seguro sin ``DATABASE_URL``.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module():
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "reconcile_untagged_adjustments", _SCRIPTS_DIR / "reconcile_untagged_adjustments.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


_T0 = datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC)
_UPLOAD_A = str(uuid.uuid4())
_UPLOAD_B = str(uuid.uuid4())
_WINDOW = 48


def _entry(upload_id: str, created_at: datetime, stock_fila, matched_on: str = "name") -> dict:
    return {
        "upload_id": upload_id,
        "upload_created_at": created_at,
        "stock_fila": stock_fila,
        "matched_on": matched_on,
    }


def _mv(name: str = "coca cola 1.5l", sku: str = "", qty=-36, created_at: datetime | None = None):
    return {
        "id": uuid.uuid4(),
        "name": name,
        "sku": sku,
        "qty": qty,
        "created_at": created_at if created_at is not None else _T0 + timedelta(hours=2),
    }


def test_r2_reconciliation_wins_over_catalog(mod):
    """R2 (auditoría) gana sobre R1: aunque matchee un upload, si el mov está en la
    auditoría → MATCHED_BACKFILL_RECONCILIATION."""
    mv = _mv()
    index = {"coca cola 1.5l": [_entry(_UPLOAD_A, _T0, 36)]}
    audit_ids = {str(mv["id"])}
    cat, ev = mod._classify_adjustment(mv, index, audit_ids, _WINDOW)
    assert cat == mod._CAT_RECON
    assert ev["audit_matched"] is True


def test_r1_catalog_exact_match(mod):
    """R1: 1 upload en ventana + qty plausible → catalog con upload_id + row_ref."""
    mv = _mv(qty=-36)
    index = {"coca cola 1.5l": [_entry(_UPLOAD_A, _T0, 36)]}
    cat, ev = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_CATALOG
    assert ev["upload_id"] == _UPLOAD_A
    assert ev["stock_fila"] == 36
    assert ev["row_ref"].startswith("reconciled:")


def test_r1_matches_by_sku(mod):
    """R1 también matchea por SKU (no solo por nombre)."""
    mv = _mv(name="producto raro", sku="SKU123", qty=10)
    index = {"sku123": [_entry(_UPLOAD_A, _T0, 20, matched_on="sku")]}
    cat, ev = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_CATALOG
    assert ev["upload_id"] == _UPLOAD_A


def test_r3_multiple_uploads_in_window(mod):
    """R3: matchea en >1 upload dentro de ventana → AMBIGUOUS."""
    mv = _mv(qty=-36)
    index = {
        "coca cola 1.5l": [
            _entry(_UPLOAD_A, _T0, 36),
            _entry(_UPLOAD_B, _T0 + timedelta(hours=1), 36),
        ]
    }
    cat, ev = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_AMBIGUOUS
    assert ev["reason"] == "matched_multiple_uploads"
    assert set(ev["uploads_in_window"]) == {_UPLOAD_A, _UPLOAD_B}


def test_r3_out_of_window(mod):
    """R3: matchea pero el upload está fuera de la ventana → AMBIGUOUS."""
    mv = _mv(qty=-36, created_at=_T0 + timedelta(hours=100))  # > 48h
    index = {"coca cola 1.5l": [_entry(_UPLOAD_A, _T0, 36)]}
    cat, ev = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_AMBIGUOUS
    assert ev["reason"] == "matched_out_of_window"


def test_r3_qty_not_plausible(mod):
    """R3: 1 upload en ventana pero qty no plausible (|qty| > stock) → AMBIGUOUS."""
    mv = _mv(qty=-500)
    index = {"coca cola 1.5l": [_entry(_UPLOAD_A, _T0, 36)]}
    cat, ev = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_AMBIGUOUS
    assert ev["reason"] == "qty_not_plausible"


def test_r4_unmatched_void(mod):
    """R4: sin rastro en documentos ni auditoría → UNMATCHED_VOID."""
    mv = _mv(name="fantasma sin documento", qty=-5)
    cat, ev = mod._classify_adjustment(mv, {}, set(), _WINDOW)
    assert cat == mod._CAT_UNMATCHED
    assert ev["reason"] == "no_document_or_audit_match"


def test_name_match_but_stock_none_never_voids(mod):
    """Borde: el producto APARECE en documentos (match de nombre) pero la fila no tiene
    stock → sin evidencia de qty → AMBIGUOUS, NUNCA UNMATCHED_VOID."""
    mv = _mv(qty=-36)
    index = {"coca cola 1.5l": [_entry(_UPLOAD_A, _T0, None)]}
    cat, ev = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_AMBIGUOUS
    assert cat != mod._CAT_UNMATCHED
    assert ev["reason"] == "qty_not_plausible"


def test_qty_equal_stock_is_plausible_positive(mod):
    """qty positiva igual al stock de la fila → plausible → catalog."""
    mv = _mv(qty=36)
    index = {"coca cola 1.5l": [_entry(_UPLOAD_A, _T0, 36)]}
    cat, _ = mod._classify_adjustment(mv, index, set(), _WINDOW)
    assert cat == mod._CAT_CATALOG


def test_compute_stock_after_positive_delta_from_negative_qty(mod):
    """qty voideada negativa (-36) → delta = -sum(qty) = +36 → sube el stock, sin clamp."""
    stock_before = 10
    qty_voided = -36
    delta = -qty_voided  # como lo arma _apply_tenant/_plan_stock_changes: delta = -Σqty
    stock_after, clamped = mod._compute_stock_after(stock_before, delta)
    assert stock_after == 46
    assert clamped is False


def test_compute_stock_after_clamps_at_zero(mod):
    """qty voideada positiva mayor al stock actual → delta negativo → clampea a 0."""
    stock_before = 2
    delta = -10  # voidear +10 unidades resta stock
    stock_after, clamped = mod._compute_stock_after(stock_before, delta)
    assert stock_after == 0
    assert clamped is True


def test_compute_stock_after_no_clamp_when_result_is_zero_exact(mod):
    """Borde: resultado exacto en 0 (no negativo) → NO es clamp."""
    stock_before = 5
    delta = -5
    stock_after, clamped = mod._compute_stock_after(stock_before, delta)
    assert stock_after == 0
    assert clamped is False
