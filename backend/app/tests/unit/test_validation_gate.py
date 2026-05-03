"""Tests para ValidationGate — puerta de calidad del pipeline de ingestión."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.application.services.validation_gate import ValidationGate


@pytest.fixture()
def gate() -> ValidationGate:
    return ValidationGate()


# ── Fixtures de summaries ──────────────────────────────────────────────────────

def _spreadsheet_ventas(confidence: str = "HIGH") -> dict:
    return {
        "file_type": "spreadsheet",
        "confidence": confidence,
        "inferred_type": "ventas",
        "has_venta": True,
        "has_fecha": True,
        "has_gasto": False,
        "has_producto": False,
        "ventas_detectadas": [
            {"fecha": date.today().strftime("%d/%m/%Y"), "monto": "50000"},
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
        "warnings": [],
    }


def _image_ocr() -> dict:
    return {
        "file_type": "image",
        "confidence": "LOW",
        "inferred_type": "ventas",
        "has_venta": False,
        "has_fecha": False,
        "ventas_detectadas": [{"linea": "Venta $50000", "montos": ["50000"]}],
        "gastos_detectados": [],
        "stock_detectado": [],
        "warnings": [],
    }


# ── Regla 1: Confidence ───────────────────────────────────────────────────────

class TestRegla1Confidence:
    def test_rejects_low_confidence_ocr(self, gate: ValidationGate) -> None:
        result = gate.validate(_image_ocr())
        assert not result.passed
        assert result.rejection_reason == "confidence_too_low"

    def test_passes_high_confidence_spreadsheet(self, gate: ValidationGate) -> None:
        result = gate.validate(_spreadsheet_ventas("HIGH"))
        assert result.passed
        assert result.rejection_reason is None

    def test_passes_medium_confidence(self, gate: ValidationGate) -> None:
        result = gate.validate(_spreadsheet_ventas("MEDIUM"))
        assert result.passed

    def test_force_override_low_confidence(self, gate: ValidationGate) -> None:
        result = gate.validate(_image_ocr(), force=True)
        assert result.passed
        corrected = result.corrected_summary or {}
        warnings = corrected.get("warnings", [])
        assert any("confidence_low_forced_by_user" in str(w) for w in warnings)


# ── Regla 2: Schema mínimo ────────────────────────────────────────────────────

class TestRegla2Schema:
    def test_rejects_ventas_sin_filas(self, gate: ValidationGate) -> None:
        summary = _spreadsheet_ventas()
        summary["ventas_detectadas"] = []
        result = gate.validate(summary)
        assert not result.passed
        assert result.rejection_reason == "schema_ventas_empty"

    def test_rejects_ventas_sin_columna_monto(self, gate: ValidationGate) -> None:
        summary = _spreadsheet_ventas()
        summary["has_venta"] = False
        result = gate.validate(summary)
        assert not result.passed
        assert result.rejection_reason == "schema_ventas_no_amount_col"

    def test_rejects_stock_sin_productos(self, gate: ValidationGate) -> None:
        summary = {
            "file_type": "spreadsheet",
            "confidence": "HIGH",
            "inferred_type": "stock",
            "has_producto": True,
            "stock_detectado": [],
            "ventas_detectadas": [],
            "gastos_detectados": [],
            "warnings": [],
        }
        result = gate.validate(summary)
        assert not result.passed
        assert result.rejection_reason == "schema_stock_empty"

    def test_rejects_general_sin_datos(self, gate: ValidationGate) -> None:
        summary = {
            "file_type": "spreadsheet",
            "confidence": "MEDIUM",
            "inferred_type": "general",
            "has_venta": False,
            "has_gasto": False,
            "has_producto": False,
            "ventas_detectadas": [],
            "gastos_detectados": [],
            "stock_detectado": [],
            "warnings": [],
        }
        result = gate.validate(summary)
        assert not result.passed
        assert result.rejection_reason == "schema_general_no_data_detected"


# ── Regla 3: Montos ───────────────────────────────────────────────────────────

class TestRegla3Montos:
    def test_negative_amount_rejected(self, gate: ValidationGate) -> None:
        summary = _spreadsheet_ventas()
        summary["ventas_detectadas"] = [
            {"fecha": date.today().strftime("%d/%m/%Y"), "monto": "-5000"},
        ]
        result = gate.validate(summary)
        assert not result.passed
        assert result.rejection_reason == "negative_amount"

    def test_large_amount_warning_not_rejected(self, gate: ValidationGate) -> None:
        summary = _spreadsheet_ventas()
        summary["ventas_detectadas"] = [
            {"fecha": date.today().strftime("%d/%m/%Y"), "monto": "60000000"},
        ]
        result = gate.validate(summary)
        assert result.passed  # no rechaza, solo warning
        assert result.corrected_summary is not None
        warnings = result.corrected_summary.get("warnings", [])
        assert any("amount_scale_warning" in str(w) for w in warnings)

    def test_normal_ars_amount_passes(self, gate: ValidationGate) -> None:
        summary = _spreadsheet_ventas()
        summary["ventas_detectadas"] = [
            {"fecha": date.today().strftime("%d/%m/%Y"), "monto": "1500000"},
        ]
        result = gate.validate(summary)
        assert result.passed


# ── Regla 4: Fechas ───────────────────────────────────────────────────────────

class TestRegla4Fechas:
    def test_passes_with_date_warnings_future(self, gate: ValidationGate) -> None:
        future_date = (date.today() + timedelta(days=10)).strftime("%d/%m/%Y")
        summary = _spreadsheet_ventas()
        summary["ventas_detectadas"] = [{"fecha": future_date, "monto": "50000"}]
        result = gate.validate(summary)
        assert result.passed
        assert result.corrected_summary is not None
        assert any("future_date_warning" in str(w) for w in result.corrected_summary.get("warnings", []))

    def test_passes_with_date_warnings_old(self, gate: ValidationGate) -> None:
        old_date = (date.today() - timedelta(days=800)).strftime("%d/%m/%Y")
        summary = _spreadsheet_ventas()
        summary["ventas_detectadas"] = [{"fecha": old_date, "monto": "50000"}]
        result = gate.validate(summary)
        assert result.passed

    def test_rejects_all_invalid_dates(self, gate: ValidationGate) -> None:
        """Si has_fecha=True y todos los valores de fecha son imparseable → rechazar."""
        summary = _spreadsheet_ventas()
        summary["has_fecha"] = True
        summary["ventas_detectadas"] = [
            {"fecha": "99/99/9999", "monto": "50000"},
            {"fecha": "32/13/2024", "monto": "10000"},
        ]
        result = gate.validate(summary)
        assert not result.passed
        assert result.rejection_reason == "all_dates_invalid"

    def test_passes_no_fecha_column(self, gate: ValidationGate) -> None:
        """Sin columna de fecha (has_fecha=False), no se rechaza por fechas."""
        summary = _spreadsheet_ventas()
        summary["has_fecha"] = False
        summary["ventas_detectadas"] = [{"monto": "50000"}]
        result = gate.validate(summary)
        assert result.passed


# ── Corrected summary ─────────────────────────────────────────────────────────

class TestCorrectedSummary:
    def test_no_correction_when_clean(self, gate: ValidationGate) -> None:
        result = gate.validate(_spreadsheet_ventas())
        assert result.passed
        assert result.corrected_summary is None  # sin cambios = None

    def test_sync_fallback_also_runs_gate(self, gate: ValidationGate) -> None:
        """Verificar que el gate rechaza correctamente sin importar el path."""
        result = gate.validate(_image_ocr(), force=False)
        assert not result.passed
        assert result.rejection_reason == "confidence_too_low"
