"""
Tests para generate_insight job — regla no-invention.

Prueba `should_skip_insight()` (función pura del código real) para garantizar
que el guard en `_run()` funciona correctamente sin copiar su lógica.
"""

from __future__ import annotations

import pytest

from app.jobs.generate_insight import should_skip_insight


# ── Tests de la función pura del guard ───────────────────────────────────────

def test_skip_when_confidence_low():
    """confidence=LOW con cualquier completitud → skip."""
    assert should_skip_insight("LOW", 90.0) is True


def test_skip_when_completeness_below_50():
    """completitud < 50 aunque confidence sea MEDIUM → skip."""
    assert should_skip_insight("MEDIUM", 49.9) is True


def test_skip_when_completeness_exactly_50():
    """completitud exactamente 50 con MEDIUM → NO skip (límite inclusivo del lado permitido)."""
    assert should_skip_insight("MEDIUM", 50.0) is False


def test_skip_when_confidence_none():
    """confidence=None se trata como LOW → skip."""
    assert should_skip_insight(None, 90.0) is True


def test_no_skip_when_medium_and_sufficient():
    """confidence=MEDIUM y completitud >= 50 → no skip."""
    assert should_skip_insight("MEDIUM", 65.0) is False


def test_no_skip_when_high_and_sufficient():
    """confidence=HIGH y completitud >= 50 → no skip."""
    assert should_skip_insight("HIGH", 80.0) is False


def test_low_confidence_overrides_high_completeness():
    """confidence=LOW con completitud=100 → skip (LOW siempre bloquea)."""
    assert should_skip_insight("LOW", 100.0) is True


def test_skip_when_zero_completeness():
    """completitud=0 (cuenta nueva) → skip."""
    assert should_skip_insight("MEDIUM", 0.0) is True
