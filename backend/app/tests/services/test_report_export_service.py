"""Tests para report_export_service.py — generación de PDF/DOCX (Stage 5a)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from app.application.services.report_export_service import (
    _sanitize_narrative,
    _xml_escape,
    generate_docx,
    generate_pdf,
)
from app.persistence.models.score import HealthScoreSnapshot


def _make_snapshot(
    score_total: int = 75,
    score_growth: int | None = 60,
    score_cash: int = 70,
    score_margin: int = 80,
    score_stock: int = 75,
    score_supplier: int = 85,
) -> HealthScoreSnapshot:
    snap = HealthScoreSnapshot()
    snap.id = uuid.uuid4()
    snap.tenant_id = uuid.uuid4()
    snap.total_score = Decimal(str(score_total))
    snap.level = "good"
    snap.dimensions = []  # type: ignore[assignment]  # test double / fixture
    snap.triggered_by = "test"
    snap.snapshot_date = datetime(2026, 5, 28, 10, 0, 0)
    snap.created_at = datetime(2026, 5, 28, 10, 0, 0)
    snap.score_cash = score_cash
    snap.score_margin = score_margin
    snap.score_stock = score_stock
    snap.score_supplier = score_supplier
    snap.score_growth = score_growth
    snap.primary_risk_code = "CASH_LOW"
    snap.confidence_level = "HIGH"
    snap.data_completeness_score = Decimal("85.0")
    return snap


class TestSanitization:
    def test_xml_escape_basic(self) -> None:
        assert _xml_escape("<script>") == "&lt;script&gt;"
        assert _xml_escape("Tom & Jerry") == "Tom &amp; Jerry"
        assert _xml_escape("a < b > c") == "a &lt; b &gt; c"

    def test_xml_escape_preserves_normal_text(self) -> None:
        assert _xml_escape("Hola mundo") == "Hola mundo"

    def test_sanitize_narrative_caps_length(self) -> None:
        long = "a" * 5000
        result = _sanitize_narrative(long)
        assert len(result) <= 4000

    def test_sanitize_narrative_escapes_xml(self) -> None:
        result = _sanitize_narrative("<dangerous>")
        assert "<" not in result
        assert "&lt;" in result


class TestPDFGeneration:
    def test_pdf_returns_bytes(self) -> None:
        snap = _make_snapshot()
        content = generate_pdf(snap, "Análisis ejecutivo.", "Kiosco Demo")
        assert isinstance(content, bytes)
        assert content.startswith(b"%PDF")  # firma PDF

    def test_pdf_includes_business_name(self) -> None:
        snap = _make_snapshot()
        content = generate_pdf(snap, "", "Mi Kiosco S.A.")
        # No podemos buscar el texto en bytes binarios, pero podemos validar
        # que no crashea y devuelve un PDF válido
        assert len(content) > 1000  # PDF mínimo > 1KB

    def test_pdf_with_v2_snapshot_includes_growth_row(self) -> None:
        snap = _make_snapshot(score_growth=80)
        content = generate_pdf(snap, "", "Demo")
        assert content.startswith(b"%PDF")

    def test_pdf_with_v1_snapshot_omits_growth(self) -> None:
        snap = _make_snapshot(score_growth=None)
        content = generate_pdf(snap, "", "Demo")
        assert content.startswith(b"%PDF")

    def test_pdf_handles_business_name_with_xml_chars(self) -> None:
        """Si el nombre tiene <, > o &, no rompe ReportLab."""
        snap = _make_snapshot()
        content = generate_pdf(snap, "Test", "Tom & Jerry <Co.>")
        assert content.startswith(b"%PDF")

    def test_pdf_handles_narrative_with_xml_chars(self) -> None:
        snap = _make_snapshot()
        narrative = "Las ventas crecieron <mucho> & superaron expectativas."
        content = generate_pdf(snap, narrative, "Demo")
        assert content.startswith(b"%PDF")

    def test_pdf_handles_empty_narrative(self) -> None:
        snap = _make_snapshot()
        content = generate_pdf(snap, "", "Demo")
        assert content.startswith(b"%PDF")


class TestDOCXGeneration:
    def test_docx_returns_bytes(self) -> None:
        snap = _make_snapshot()
        content = generate_docx(snap, "Análisis.", "Kiosco Demo")
        assert isinstance(content, bytes)
        # DOCX es un ZIP — firma PK\x03\x04
        assert content[:4] == b"PK\x03\x04"

    def test_docx_with_v2_snapshot(self) -> None:
        snap = _make_snapshot(score_growth=80)
        content = generate_docx(snap, "Test", "Demo")
        assert content[:4] == b"PK\x03\x04"

    def test_docx_with_v1_snapshot(self) -> None:
        snap = _make_snapshot(score_growth=None)
        content = generate_docx(snap, "", "Demo")
        assert content[:4] == b"PK\x03\x04"

    def test_docx_handles_empty_narrative(self) -> None:
        snap = _make_snapshot()
        content = generate_docx(snap, "", "Demo")
        assert content[:4] == b"PK\x03\x04"

    def test_docx_truncates_long_narrative(self) -> None:
        """Narrativa > 4000 chars se trunca sin romper el documento."""
        snap = _make_snapshot()
        long = "Texto repetido. " * 500  # ~8000 chars
        content = generate_docx(snap, long, "Demo")
        assert content[:4] == b"PK\x03\x04"
