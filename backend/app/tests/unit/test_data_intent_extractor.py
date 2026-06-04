from app.application.services.data_intent_extractor import DataIntentExtractor


def test_detecta_venta_desde_inferred_type_ventas() -> None:
    result = DataIntentExtractor().check_file_summary(
        {
            "inferred_type": "ventas",
            "confidence": "HIGH",
            "ventas_detectadas": [{"monto": "1000"}],
        }
    )
    assert result.has_data_intent is True
    assert result.intent_type == "sale"


def test_no_detecta_si_ventas_detectadas_vacio() -> None:
    result = DataIntentExtractor().check_file_summary(
        {
            "inferred_type": "ventas",
            "confidence": "HIGH",
            "ventas_detectadas": [],
        }
    )
    assert result.has_data_intent is False
    assert result.intent_type is None


def test_detecta_gasto() -> None:
    result = DataIntentExtractor().check_file_summary(
        {
            "inferred_type": "gastos",
            "confidence": "MEDIUM",
            "gastos_detectados": [{"monto": "500"}],
        }
    )
    assert result.has_data_intent is True
    assert result.intent_type == "expense"


def test_detecta_stock() -> None:
    result = DataIntentExtractor().check_file_summary(
        {
            "inferred_type": "stock",
            "confidence": "HIGH",
            "stock_detectado": [{"producto": "Yerba"}],
        }
    )
    assert result.has_data_intent is True
    assert result.intent_type == "product"


def test_general_sin_filas_no_es_dato() -> None:
    result = DataIntentExtractor().check_file_summary({"inferred_type": "general"})
    assert result.has_data_intent is False
    assert result.confidence == "LOW"


def test_general_con_ventas_detectadas_es_sale() -> None:
    result = DataIntentExtractor().check_file_summary(
        {"inferred_type": "general", "ventas_detectadas": [{"monto": "500"}]}
    )
    assert result.has_data_intent is True
    assert result.intent_type == "sale"
    assert result.confidence == "LOW"


def test_general_con_stock_detectado_es_product() -> None:
    result = DataIntentExtractor().check_file_summary(
        {"inferred_type": "general", "stock_detectado": [{"nombre": "Coca Cola"}]}
    )
    assert result.has_data_intent is True
    assert result.intent_type == "product"


def test_confidence_propaga_del_summary() -> None:
    result = DataIntentExtractor().check_file_summary(
        {
            "inferred_type": "ventas",
            "confidence": "MEDIUM",
            "ventas_detectadas": [{"monto": "1000"}],
        }
    )
    assert result.confidence == "MEDIUM"
