"""Tests unitarios de ``_classify_cluster`` en scripts/repair_inventory_ledger.py.

No depende de DB: ``_classify_cluster`` es una función pura sobre una lista de
"miembros" (mappings con id/source_row_hash/source_upload_id/created_at). Se carga
el módulo por ruta de archivo (``scripts/`` no es un paquete) en vez de importarlo
como parte de ``app``.
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
        "repair_inventory_ledger", _SCRIPTS_DIR / "repair_inventory_ledger.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _member(source_upload_id: str | None, created_at: datetime, source_row_hash: str | None = None):
    return {
        "id": uuid.uuid4(),
        "source_row_hash": source_row_hash,
        "source_upload_id": source_upload_id,
        "created_at": created_at,
    }


_T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)


def test_both_untagged_tight_timing_is_high_confidence(mod):
    """Comportamiento original sin cambios: sin source_upload_id, timing < 5s = HIGH."""
    members = [_member(None, _T0), _member(None, _T0 + timedelta(seconds=1))]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._HIGH
    assert reason == mod._REASON_TIGHT_TIMING
    assert void_ids == [str(members[1]["id"])]


def test_both_untagged_large_delta_is_pending(mod):
    """Sin tag, delta grande: pasa a PENDING (el pase 2 decide BATCH_TIMING vs LOW)."""
    members = [_member(None, _T0), _member(None, _T0 + timedelta(hours=2))]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._PENDING
    assert reason is None
    assert delta == pytest.approx(7200.0)


def test_both_import_tight_timing_is_inconclusive_not_duplicate(mod):
    """El caso que motivó el fix: mismo origen archivo + timing chico != duplicado."""
    upload_id = str(uuid.uuid4())
    members = [
        _member(upload_id, _T0),
        _member(str(uuid.uuid4()), _T0 + timedelta(milliseconds=50)),
    ]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._LOW
    assert reason == mod._REASON_IMPORT_BATCH_TIMING_INCONCLUSIVE
    assert void_ids == []


def test_both_import_large_delta_is_also_inconclusive_never_pending(mod):
    """Import+import nunca debe llegar a PENDING/BATCH_TIMING, ni con delta grande."""
    members = [
        _member(str(uuid.uuid4()), _T0),
        _member(str(uuid.uuid4()), _T0 + timedelta(hours=5)),
    ]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._LOW
    assert reason == mod._REASON_IMPORT_BATCH_TIMING_INCONCLUSIVE


def test_mixed_origin_is_medium_review(mod):
    """Un lado tagueado y el otro no: revisión humana, no auto-clasificado."""
    members = [_member(str(uuid.uuid4()), _T0), _member(None, _T0 + timedelta(seconds=1))]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._MEDIUM
    assert reason == mod._REASON_MIXED_ORIGIN_REVIEW
    assert void_ids == []


def test_shared_row_hash_is_high_regardless_of_origin(mod):
    """SHARED_ROW_HASH (paso 1) sigue ganando sin importar source_upload_id."""
    h = "abc123"
    members = [
        _member(str(uuid.uuid4()), _T0, source_row_hash=h),
        _member(str(uuid.uuid4()), _T0 + timedelta(hours=3), source_row_hash=h),
    ]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._HIGH
    assert reason == mod._REASON_SHARED_HASH
    assert void_ids == [str(members[1]["id"])]


def test_triplicate_same_day_is_high_regardless_of_origin(mod):
    """TRIPLICATE_SAME_DAY (paso 2, n>=3) sigue ganando sin importar origen."""
    members = [
        _member(str(uuid.uuid4()), _T0),
        _member(None, _T0 + timedelta(seconds=2)),
        _member(str(uuid.uuid4()), _T0 + timedelta(seconds=4)),
    ]
    confidence, reason, void_ids, delta = mod._classify_cluster(members)
    assert confidence == mod._HIGH
    assert reason == mod._REASON_TRIPLICATE
    assert void_ids == [str(members[1]["id"]), str(members[2]["id"])]
