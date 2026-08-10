"""Tests del rescate determinístico de intents ambiguos (Sprint 17, Stage 0.5)."""

import pytest

from app.application.agents.shared.intent_rescue import normalize, rescue_intent

# ── Normalización (ajuste #4: voseo + tildes + signos) ────────────────────────


def test_normalize_voseo_and_accents():
    assert normalize("Analizá esta lísta de précios!") == "analiza esta lista de precios"
    assert normalize("Mirá") == "mira"
    assert normalize("Chequeame el stock") == "chequea el stock"


# ── Tabla de rescate: mensaje + adjunto → (intent consolidado, entities) ──────
#
# Sprint 19: rescue_intent devuelve (intent_consolidado, entities con analysis_type).
# Los ids conservan el nombre del test original de cada caso (12 reglas de
# prioridad, gate de clasificación de gastos, fuzzy matching, verbos de import
# y verbos de búsqueda).


@pytest.mark.parametrize(
    ("mensaje", "tiene_adjunto", "tipo_adjunto", "esperado"),
    [
        # ── 12 casos de ambigüedad (uno por regla de prioridad) ───────────────
        pytest.param(
            "analizá esto",
            True,
            "product",
            ("analizar_precios", {"analysis_type": "lista"}),
            id="test_rule1_attachment_product_plus_verb",
        ),
        pytest.param(
            "mirá esto",
            True,
            "stock",
            ("analizar_stock", {}),
            id="test_rule2_attachment_stock",
        ),
        pytest.param(
            "revisá esto",
            True,
            "sale",
            ("analizar_archivo", {}),
            id="test_rule3_attachment_sale_mixed_sale",
        ),
        pytest.param(
            "fijate",
            True,
            "mixed",
            ("analizar_archivo", {}),
            id="test_rule3_attachment_sale_mixed_mixed",
        ),
        pytest.param(
            "chequeá esto",
            True,
            None,
            ("analizar_archivo", {}),
            id="test_rule4_attachment_no_type_with_verb",
        ),
        pytest.param(
            "cuánto gano con cada producto",
            False,
            None,
            ("analizar_precios", {"analysis_type": "margenes"}),
            id="test_rule5_objects_margen_gano",
        ),
        pytest.param(
            "cuál es mi rentabilidad",
            False,
            None,
            ("analizar_precios", {"analysis_type": "margenes"}),
            id="test_rule5_objects_margen_rentabilidad",
        ),
        pytest.param(
            "el proveedor me mandó un aumento",
            False,
            None,
            ("analizar_precios", {"analysis_type": "aumentos_proveedor"}),
            id="test_rule6_proveedor_aumento_remarcar_aumento",
        ),
        pytest.param(
            "tengo que remarcar todo",
            False,
            None,
            ("analizar_precios", {"analysis_type": "simulacion"}),
            id="test_rule6_proveedor_aumento_remarcar_remarcar",
        ),
        pytest.param(
            "mis proveedores",
            False,
            None,
            ("analizar_proveedores", {}),
            id="test_rule6_proveedor_aumento_remarcar_proveedores",
        ),
        pytest.param(
            "cuánto me queda de plata",
            False,
            None,
            ("proyectar_caja", {}),
            id="test_rule7_objects_caja_plata",
        ),
        pytest.param(
            "cómo está mi liquidez",
            False,
            None,
            ("proyectar_caja", {}),
            id="test_rule7_objects_caja_liquidez",
        ),
        pytest.param(
            "qué me falta reponer",
            False,
            None,
            ("analizar_stock", {"analysis_type": "quiebres"}),
            id="test_rule8_objects_stock_reponer",
        ),
        pytest.param(
            "cómo está mi inventario",
            False,
            None,
            ("analizar_stock", {"analysis_type": "quiebres"}),
            id="test_rule8_objects_stock_inventario",
        ),
        pytest.param(
            "cómo viene el negocio",
            False,
            None,
            ("consultar_estado_negocio", {}),
            id="test_rule9_verbs_negocio",
        ),
        pytest.param(
            "qué puedo hacer con esto",
            True,
            None,
            ("ayuda_plataforma", {"analysis_type": "explicar_datos"}),
            id="test_rule10_que_puedo_hacer_with_attachment",
        ),
        # verbo ambiguo sin objeto de negocio ni adjunto → pedir aclaración de negocio
        pytest.param(
            "analizá",
            False,
            None,
            ("pedir_aclaracion_negocio", {}),
            id="test_rule11_ambiguous_verb_no_object",
        ),
        # ajuste #1: claramente off-topic → out_of_scope, NO aclaración financiera
        pytest.param(
            "contame un chiste",
            False,
            None,
            ("out_of_scope", {}),
            id="test_rule12_off_topic_stays_out_of_scope_chiste",
        ),
        pytest.param(
            "quién ganó el mundial",
            False,
            None,
            ("out_of_scope", {}),
            id="test_rule12_off_topic_stays_out_of_scope_mundial",
        ),
        # ── Clasificación/categorización de gastos: nunca out_of_scope ────────
        # "esto es mercadería o insumo?" → consejo de clasificación de gasto, NO
        # out_of_scope ni stock (la palabra "mercadería" no debe rutear a quiebres
        # en este contexto).
        pytest.param(
            "esto es mercadería o insumo?",
            False,
            None,
            ("analizar_gastos", {"analysis_type": "clasificacion"}),
            id="test_clasificacion_mercaderia_o_insumo_no_out_of_scope",
        ),
        pytest.param(
            "¿cómo clasificarías este gasto de $3000?",
            False,
            None,
            ("analizar_gastos", {"analysis_type": "clasificacion"}),
            id="test_clasificacion_como_clasificarias_gasto_no_out_of_scope",
        ),
        # sin objeto de negocio explícito ni verbo ambiguo: antes caía en out_of_scope.
        pytest.param(
            "reclasificar este movimiento",
            False,
            None,
            ("analizar_gastos", {"analysis_type": "clasificacion"}),
            id="test_clasificacion_reclasificar_movimiento_no_out_of_scope",
        ),
        # el gate de clasificación NO debe ablandar off-topic genuino.
        pytest.param(
            "contame un chiste",
            False,
            None,
            ("out_of_scope", {}),
            id="test_off_topic_real_still_out_of_scope",
        ),
        # ── Tolerancia a typos (fuzzy matching) ───────────────────────────────
        pytest.param(
            "presios de la lista",
            False,
            None,
            ("analizar_precios", {}),
            id="test_fuzzy_typos_presios",
        ),
        pytest.param(
            "revisá el stok",
            False,
            None,
            ("analizar_stock", {"analysis_type": "quiebres"}),
            id="test_fuzzy_typos_stok",
        ),
        # adjunto sin tipo ni verbo → analizar_archivo (mejor que preguntar; el
        # agente puede orientar)
        pytest.param(
            "xyz",
            True,
            None,
            ("analizar_archivo", {}),
            id="test_attachment_no_verb_no_type_falls_back_to_analyze",
        ),
        # ── Verbos de importación con adjunto → intents de import ─────────────
        pytest.param(
            "importá y anotá esto",
            True,
            "product",
            ("importar_archivo_productos", {}),
            id="test_import_verbs_with_attachment_route_to_import_intents_producto",
        ),
        pytest.param(
            "cargá estos registros",
            True,
            "expense",
            ("importar_archivo_gastos", {}),
            id="test_import_verbs_with_attachment_route_to_import_intents_gasto",
        ),
        pytest.param(
            "registrá lo de la planilla",
            True,
            "sale",
            ("importar_archivo_ventas", {}),
            id="test_import_verbs_with_attachment_route_to_import_intents_venta",
        ),
        pytest.param(
            "guardá estos datos",
            True,
            "mixed",
            ("importar_archivo_ventas", {}),
            id="test_import_verbs_with_attachment_route_to_import_intents_mixed",
        ),
        pytest.param(
            "subí el archivo",
            True,
            None,
            ("importar_archivo_ventas", {}),
            id="test_import_verbs_with_attachment_route_to_import_intents_sin_tipo",
        ),
        pytest.param(
            "importalo",
            True,
            "product",
            ("importar_archivo_productos", {}),
            id="test_import_verbs_with_attachment_route_to_import_intents_importalo",
        ),
        # ── Workstream C4: verbos de búsqueda/detección → reclasificar_gasto ──
        pytest.param(
            "detectá los registros de revistas para reclasificar",
            False,
            None,
            ("reclasificar_gasto", {"search": "true"}),
            id="test_search_verb_detecta_registros_routes_to_reclassify",
        ),
        pytest.param(
            "buscá los gastos de La Nación",
            False,
            None,
            ("reclasificar_gasto", {"search": "true"}),
            id="test_search_verb_busca_gastos_routes_to_reclassify",
        ),
        pytest.param(
            "encontrá los movimientos de diarios",
            False,
            None,
            ("reclasificar_gasto", {"search": "true"}),
            id="test_search_verb_encontra_movimientos_routes_to_reclassify",
        ),
        pytest.param(
            "mostrame los gastos de mercadería",
            False,
            None,
            ("reclasificar_gasto", {"search": "true"}),
            id="test_search_verb_mostrame_gastos_mercaderia_routes_to_reclassify",
        ),
    ],
)
def test_rescue_intent_tabla(mensaje, tiene_adjunto, tipo_adjunto, esperado):
    assert rescue_intent(mensaje, tiene_adjunto, tipo_adjunto) == esperado


def test_clasificacion_screenshot_real_revista_mercaderia():
    # Caso real de la screenshot: el agente respondía "fuera de mis competencias".
    result = rescue_intent(
        "Revistas Diario La Nación Mercadería $3.000 ¿cómo lo clasificarías?",
        False,
        None,
    )
    assert result != ("out_of_scope", {})
    assert result == ("analizar_gastos", {"analysis_type": "clasificacion"})


def test_search_verb_without_object_does_not_route_to_reclassify():
    # verbo de búsqueda sin objeto de gasto/reclasificación → NO reclasificar_gasto
    intent, _ = rescue_intent("mostrame", False, None)
    assert intent != "reclasificar_gasto"
