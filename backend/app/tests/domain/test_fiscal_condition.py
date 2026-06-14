"""Tests del normalizador de condición fiscal (app/domain/fiscal_condition.py)."""

from unittest.mock import patch

import pytest

from app.domain.fiscal_condition import (
    CANONICAL_FISCAL_CONDITIONS,
    INFORMAL,
    MONOTRIBUTO,
    RESPONSABLE_INSCRIPTO,
    is_registered,
    normalize_fiscal_condition,
)


class TestNormalizeFiscalCondition:
    def test_none_and_empty(self) -> None:
        assert normalize_fiscal_condition(None) is None
        assert normalize_fiscal_condition("") is None
        assert normalize_fiscal_condition("   ") is None

    def test_canonical_values_passthrough(self) -> None:
        assert normalize_fiscal_condition("monotributo") == MONOTRIBUTO
        assert (
            normalize_fiscal_condition("responsable_inscripto") == RESPONSABLE_INSCRIPTO
        )
        assert normalize_fiscal_condition("informal") == INFORMAL

    def test_legacy_registered_maps_to_monotributo(self) -> None:
        # Compatibilidad: el legacy 'registered' (monotributo/RI) → monotributo.
        assert normalize_fiscal_condition("registered") == MONOTRIBUTO

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Monotributo", MONOTRIBUTO),
            ("MONO", MONOTRIBUTO),
            ("responsable inscripto", RESPONSABLE_INSCRIPTO),
            ("Responsable Inscripta", RESPONSABLE_INSCRIPTO),
            ("RI", RESPONSABLE_INSCRIPTO),
            ("no inscripto", INFORMAL),
            ("sin inscripcion", INFORMAL),
        ],
    )
    def test_tolerant_aliases(self, raw: str, expected: str) -> None:
        assert normalize_fiscal_condition(raw) == expected

    def test_unknown_returns_none(self) -> None:
        # Fail-soft: valor desconocido no se inventa (es informativo).
        assert normalize_fiscal_condition("cualquier_cosa") is None

    def test_unknown_value_emits_warning(self) -> None:
        # No vaciar en silencio: un valor no vacío no reconocido se loguea.
        with patch("app.domain.fiscal_condition.logger") as mock_logger:
            assert normalize_fiscal_condition("cualquier_cosa") is None
            mock_logger.warning.assert_called_once()
            args, kwargs = mock_logger.warning.call_args
            assert args[0] == "fiscal_condition_unrecognized"
            assert kwargs["normalized_key"] == "cualquier_cosa"

    def test_empty_and_none_do_not_warn(self) -> None:
        # None / cadena vacía son "no configurado", no señal de dato corrupto.
        with patch("app.domain.fiscal_condition.logger") as mock_logger:
            assert normalize_fiscal_condition(None) is None
            assert normalize_fiscal_condition("   ") is None
            mock_logger.warning.assert_not_called()

    def test_recognized_value_does_not_warn(self) -> None:
        with patch("app.domain.fiscal_condition.logger") as mock_logger:
            assert normalize_fiscal_condition("registered") == MONOTRIBUTO
            mock_logger.warning.assert_not_called()

    def test_canonical_set_has_three_values(self) -> None:
        assert {
            MONOTRIBUTO,
            RESPONSABLE_INSCRIPTO,
            INFORMAL,
        } == CANONICAL_FISCAL_CONDITIONS


class TestIsRegistered:
    def test_registered_values(self) -> None:
        assert is_registered("monotributo") is True
        assert is_registered("responsable_inscripto") is True
        assert is_registered("registered") is True  # legacy
        assert is_registered("RI") is True

    def test_not_registered_values(self) -> None:
        assert is_registered("informal") is False
        assert is_registered(None) is False
        assert is_registered("desconocido") is False
