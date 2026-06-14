"""Tests del rescate determinístico de intents ambiguos (Sprint 17, Stage 0.5)."""

from app.application.agents.shared.intent_rescue import normalize, rescue_intent

# ── Normalización (ajuste #4: voseo + tildes + signos) ────────────────────────


def test_normalize_voseo_and_accents():
    assert normalize("Analizá esta lísta de précios!") == "analiza esta lista de precios"
    assert normalize("Mirá") == "mira"
    assert normalize("Chequeame el stock") == "chequea el stock"


# ── 12 casos de ambigüedad (uno por regla de prioridad) ───────────────────────


# Sprint 19: rescue_intent devuelve (intent_consolidado, entities con analysis_type).


def test_rule1_attachment_product_plus_verb():
    assert rescue_intent("analizá esto", True, "product") == (
        "analizar_precios",
        {"analysis_type": "lista"},
    )


def test_rule2_attachment_stock():
    assert rescue_intent("mirá esto", True, "stock") == ("analizar_stock", {})


def test_rule3_attachment_sale_mixed():
    assert rescue_intent("revisá esto", True, "sale") == ("analizar_archivo", {})
    assert rescue_intent("fijate", True, "mixed") == ("analizar_archivo", {})


def test_rule4_attachment_no_type_with_verb():
    assert rescue_intent("chequeá esto", True, None) == ("analizar_archivo", {})


def test_rule5_objects_margen():
    assert rescue_intent("cuánto gano con cada producto", False, None) == (
        "analizar_precios",
        {"analysis_type": "margenes"},
    )
    assert rescue_intent("cuál es mi rentabilidad", False, None) == (
        "analizar_precios",
        {"analysis_type": "margenes"},
    )


def test_rule6_proveedor_aumento_remarcar():
    assert rescue_intent("el proveedor me mandó un aumento", False, None) == (
        "analizar_precios",
        {"analysis_type": "aumentos_proveedor"},
    )
    assert rescue_intent("tengo que remarcar todo", False, None) == (
        "analizar_precios",
        {"analysis_type": "simulacion"},
    )
    assert rescue_intent("mis proveedores", False, None) == ("analizar_proveedores", {})


def test_rule7_objects_caja():
    assert rescue_intent("cuánto me queda de plata", False, None) == ("proyectar_caja", {})
    assert rescue_intent("cómo está mi liquidez", False, None) == ("proyectar_caja", {})


def test_rule8_objects_stock():
    assert rescue_intent("qué me falta reponer", False, None) == (
        "analizar_stock",
        {"analysis_type": "quiebres"},
    )
    assert rescue_intent("cómo está mi inventario", False, None) == (
        "analizar_stock",
        {"analysis_type": "quiebres"},
    )


def test_rule9_verbs_negocio():
    assert rescue_intent("cómo viene el negocio", False, None) == ("consultar_estado_negocio", {})


def test_rule10_que_puedo_hacer_with_attachment():
    assert rescue_intent("qué puedo hacer con esto", True, None) == (
        "ayuda_plataforma",
        {"analysis_type": "explicar_datos"},
    )


def test_rule11_ambiguous_verb_no_object():
    # verbo ambiguo sin objeto de negocio ni adjunto → pedir aclaración de negocio
    assert rescue_intent("analizá", False, None) == ("pedir_aclaracion_negocio", {})


def test_rule12_off_topic_stays_out_of_scope():
    # ajuste #1: claramente off-topic → out_of_scope, NO aclaración financiera
    assert rescue_intent("contame un chiste", False, None) == ("out_of_scope", {})
    assert rescue_intent("quién ganó el mundial", False, None) == ("out_of_scope", {})


# ── Clasificación/categorización de gastos: nunca out_of_scope ────────────────


def test_clasificacion_mercaderia_o_insumo_no_out_of_scope():
    # "esto es mercadería o insumo?" → consejo de clasificación de gasto, NO out_of_scope
    # ni stock (la palabra "mercadería" no debe rutear a quiebres en este contexto).
    assert rescue_intent("esto es mercadería o insumo?", False, None) == (
        "analizar_gastos",
        {"analysis_type": "clasificacion"},
    )


def test_clasificacion_como_clasificarias_gasto_no_out_of_scope():
    assert rescue_intent("¿cómo clasificarías este gasto de $3000?", False, None) == (
        "analizar_gastos",
        {"analysis_type": "clasificacion"},
    )


def test_clasificacion_reclasificar_movimiento_no_out_of_scope():
    # sin objeto de negocio explícito ni verbo ambiguo: antes caía en out_of_scope.
    assert rescue_intent("reclasificar este movimiento", False, None) == (
        "analizar_gastos",
        {"analysis_type": "clasificacion"},
    )


def test_clasificacion_screenshot_real_revista_mercaderia():
    # Caso real de la screenshot: el agente respondía "fuera de mis competencias".
    result = rescue_intent(
        "Revistas Diario La Nación Mercadería $3.000 ¿cómo lo clasificarías?",
        False,
        None,
    )
    assert result != ("out_of_scope", {})
    assert result == ("analizar_gastos", {"analysis_type": "clasificacion"})


def test_off_topic_real_still_out_of_scope():
    # el gate de clasificación NO debe ablandar off-topic genuino.
    assert rescue_intent("contame un chiste", False, None) == ("out_of_scope", {})


# ── Tolerancia a typos (fuzzy matching) ───────────────────────────────────────


def test_fuzzy_typos():
    assert rescue_intent("presios de la lista", False, None) == ("analizar_precios", {})
    assert rescue_intent("revisá el stok", False, None) == (
        "analizar_stock",
        {"analysis_type": "quiebres"},
    )


def test_attachment_no_verb_no_type_falls_back_to_analyze():
    # adjunto sin tipo ni verbo → analizar_archivo (mejor que preguntar; el agente puede orientar)
    assert rescue_intent("xyz", True, None) == ("analizar_archivo", {})


def test_import_verbs_with_attachment_route_to_import_intents():
    assert rescue_intent("importá y anotá esto", True, "product") == (
        "importar_archivo_productos",
        {},
    )
    assert rescue_intent("cargá estos registros", True, "expense") == (
        "importar_archivo_gastos",
        {},
    )
    assert rescue_intent("registrá lo de la planilla", True, "sale") == (
        "importar_archivo_ventas",
        {},
    )
    assert rescue_intent("guardá estos datos", True, "mixed") == (
        "importar_archivo_ventas",
        {},
    )
    assert rescue_intent("subí el archivo", True, None) == ("importar_archivo_ventas", {})
    assert rescue_intent("importalo", True, "product") == ("importar_archivo_productos", {})
