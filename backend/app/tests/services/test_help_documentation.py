"""Tests para HelpDocumentationService — Stage 5b."""

from app.application.services.help_documentation_service import (
    format_faq_context,
    is_business_data_question,
    search,
)


class TestSearch:
    def test_returns_empty_when_no_match(self) -> None:
        matches = search("xyzturbochicken")
        assert isinstance(matches, list)

    def test_finds_venta_faq(self) -> None:
        matches = search("cómo cargo una venta")
        assert len(matches) >= 1
        texts = " ".join(m.question.lower() + m.answer.lower() for m in matches)
        assert "venta" in texts

    def test_finds_health_score_faq(self) -> None:
        matches = search("qué es el health score")
        assert len(matches) >= 1

    def test_finds_google_connection_faq(self) -> None:
        matches = search("cómo conecto Google")
        assert len(matches) >= 1

    def test_importar_matches_cargar_faq_via_synonyms(self) -> None:
        # "importo un Excel" debe matchear la FAQ "cómo cargo un archivo (Excel…)".
        matches = search("¿Cómo importo un Excel?")
        assert len(matches) >= 1
        texts = " ".join(m.question.lower() + " " + m.answer.lower() for m in matches)
        assert "archivo" in texts or "excel" in texts

    def test_subir_planilla_matches_via_synonyms(self) -> None:
        matches = search("cómo subo una planilla")
        assert len(matches) >= 1

    def test_max_results_respected(self) -> None:
        matches = search("ventas gastos stock proveedor", max_results=2)
        assert len(matches) <= 2

    def test_module_populated(self) -> None:
        matches = search("cómo cargo una venta")
        for m in matches:
            assert m.module != ""

    def test_format_faq_context_nonempty(self) -> None:
        matches = search("cómo cargo una venta")
        ctx = format_faq_context(matches)
        assert "DOCUMENTACIÓN" in ctx
        assert "venta" in ctx.lower()

    def test_format_faq_context_empty_list(self) -> None:
        assert format_faq_context([]) == ""


class TestIsBusinessDataQuestion:
    def test_detects_sale_keywords(self) -> None:
        assert is_business_data_question("vendí 3 gaseosas") is True
        assert is_business_data_question("registré una venta") is True

    def test_detects_expense_action_verbs(self) -> None:
        """Verbos en pasado son acciones de negocio."""
        assert is_business_data_question("pagué el alquiler") is True
        assert is_business_data_question("gasté en mercadería") is True
        assert is_business_data_question("compré 3 unidades") is True

    def test_platform_question_not_business(self) -> None:
        """Preguntas con marcadores 'cómo/qué' nunca son acción de negocio."""
        assert is_business_data_question("cómo conecto Google") is False
        assert is_business_data_question("qué es el health score") is False
        # Estos son críticos: tienen verbos de acción pero también palabras de pregunta
        assert is_business_data_question("¿cómo cargo una venta?") is False
        assert is_business_data_question("¿cómo veo mi inventario?") is False

    def test_empty_string(self) -> None:
        assert is_business_data_question("") is False
