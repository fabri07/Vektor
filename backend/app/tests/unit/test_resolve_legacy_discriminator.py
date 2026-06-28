"""Tests unitarios de _resolve_legacy_discriminator por familia analítica.

Cubre los 3 casos para CADA familia en _ANALYTIC_FAMILIES:
1. analysis_type ausente (None o no en entities) → retorna el default
2. analysis_type inválido (string no reconocido) → retorna el default
3. analysis_type válido → retorna el mapeo correcto
"""

import pytest

from app.application.agents.ceo.team_plan_builder import (
    _ANALYTIC_FAMILIES,
    _resolve_legacy_discriminator,
)


class TestResolveLegacyDiscriminatorByFamily:
    """Tests parametrizados por familia analítica en _ANALYTIC_FAMILIES."""

    @pytest.fixture
    def families(self) -> dict[str, dict]:
        """Carga las familias desde el módulo."""
        return _ANALYTIC_FAMILIES

    @pytest.mark.parametrize(
        "family_key",
        [
            "analizar_precios",
            "analizar_stock",
            "analizar_ventas",
            "analizar_clientes",
            "analizar_gastos",
            "analizar_proveedores",
            "proyectar_caja",
            "analizar_archivo",
            "ayuda_plataforma",
        ],
    )
    def test_absent_analysis_type_returns_default(self, family_key: str, families: dict):
        """Sin analysis_type (None o ausente), retorna el default de la familia."""
        family = families[family_key]
        expected_default = family["default"]

        # Case 1: analysis_type es None explícitamente
        result = _resolve_legacy_discriminator(family_key, None)
        assert result == expected_default, (
            f"Family {family_key}: None analysis_type debe retornar default "
            f"{expected_default!r}, obtuvo {result!r}"
        )

        # Case 2: analysis_type es un string vacío (inválido)
        result = _resolve_legacy_discriminator(family_key, "")
        assert result == expected_default, (
            f"Family {family_key}: empty string analysis_type debe retornar default "
            f"{expected_default!r}, obtuvo {result!r}"
        )

        # Case 3: analysis_type es un int (tipo no esperado)
        result = _resolve_legacy_discriminator(family_key, 42)
        assert result == expected_default, (
            f"Family {family_key}: int analysis_type debe retornar default "
            f"{expected_default!r}, obtuvo {result!r}"
        )

    @pytest.mark.parametrize(
        "family_key",
        [
            "analizar_precios",
            "analizar_stock",
            "analizar_ventas",
            "analizar_clientes",
            "analizar_gastos",
            "analizar_proveedores",
            "proyectar_caja",
            "analizar_archivo",
            "ayuda_plataforma",
        ],
    )
    def test_invalid_analysis_type_returns_default(self, family_key: str, families: dict):
        """analysis_type inválido (no en types) retorna el default."""
        family = families[family_key]
        expected_default = family["default"]

        # String que definitivamente no está en los tipos válidos
        invalid_type = "no_existe_nunca_en_tipos"
        result = _resolve_legacy_discriminator(family_key, invalid_type)
        assert result == expected_default, (
            f"Family {family_key}: invalid analysis_type {invalid_type!r} debe retornar "
            f"default {expected_default!r}, obtuvo {result!r}"
        )

        # Otro inválido
        result = _resolve_legacy_discriminator(family_key, "inventado_xyz")
        assert result == expected_default

    @pytest.mark.parametrize(
        "family_key,analysis_type,expected_intent",
        [
            # analizar_precios (7 tipos)
            ("analizar_precios", "margenes", "analizar_margenes_productos"),
            ("analizar_precios", "sugerencia", "sugerir_precios_venta"),
            ("analizar_precios", "comparacion", "comparar_listas_precios"),
            ("analizar_precios", "aumentos_proveedor", "detectar_aumentos_proveedor"),
            ("analizar_precios", "simulacion", "simular_actualizacion_precios"),
            ("analizar_precios", "sensibilidad", "identificar_productos_sensibles"),
            ("analizar_precios", "lista", "analizar_lista_precios"),
            # analizar_stock (5 tipos)
            ("analizar_stock", "general", "analizar_stock"),
            ("analizar_stock", "quiebres", "detectar_quiebres_stock"),
            ("analizar_stock", "sobrestock", "detectar_sobrestock"),
            ("analizar_stock", "reposicion", "priorizar_reposicion"),
            ("analizar_stock", "duracion", "estimar_dias_stock"),
            # analizar_ventas (5 tipos)
            ("analizar_ventas", "rentabilidad", "analizar_rentabilidad_ventas"),
            ("analizar_ventas", "estrella", "detectar_productos_estrella"),
            ("analizar_ventas", "problematicos", "detectar_productos_problematicos"),
            ("analizar_ventas", "ticket", "analizar_ticket_promedio"),
            ("analizar_ventas", "descuentos", "analizar_descuentos"),
            # analizar_clientes (4 tipos)
            ("analizar_clientes", "general", "analizar_clientes"),
            ("analizar_clientes", "inactivos", "detectar_clientes_inactivos"),
            ("analizar_clientes", "cuentas_por_cobrar", "analizar_cuentas_por_cobrar"),
            ("analizar_clientes", "cobranza", "priorizar_cobranza"),
            # analizar_gastos (5 tipos)
            ("analizar_gastos", "clasificacion", "clasificar_gastos"),
            ("analizar_gastos", "recurrentes", "detectar_gastos_recurrentes"),
            ("analizar_gastos", "anomalos", "detectar_gastos_anomalos"),
            ("analizar_gastos", "costos_fijos_variables", "analizar_costos_fijos_variables"),
            ("analizar_gastos", "punto_equilibrio", "calcular_punto_equilibrio"),
            # analizar_proveedores (3 tipos). El sub-tipo `pedido_sugerido` se
            # promovió a intent top-level `preparar_pedido_sugerido` en F3 (Véktor v4)
            # → ya NO es un sub-análisis de esta familia, por eso no figura acá.
            ("analizar_proveedores", "ranking", "analizar_proveedores"),
            ("analizar_proveedores", "comparacion_precios", "comparar_precios_proveedores"),
            ("analizar_proveedores", "dependencia", "detectar_dependencia_proveedor"),
            # proyectar_caja (3 tipos)
            ("proyectar_caja", "proyeccion", "proyectar_caja"),
            ("proyectar_caja", "alerta_liquidez", "alertar_falta_liquidez"),
            ("proyectar_caja", "what_if", "simular_escenario_financiero"),
            # analizar_archivo (4 tipos)
            ("analizar_archivo", "contenido", "analizar_archivo_cargado"),
            ("analizar_archivo", "tipo", "detectar_tipo_archivo"),
            ("analizar_archivo", "limpieza", "limpiar_normalizar_archivo"),
            ("analizar_archivo", "resumen", "resumen_ejecutivo_archivo"),
            # ayuda_plataforma (2 tipos)
            ("ayuda_plataforma", "con_archivo", "ayudar_con_archivo"),
            ("ayuda_plataforma", "explicar_datos", "explicar_que_puedo_hacer_con_datos"),
        ],
    )
    def test_valid_analysis_type_returns_mapped_intent(
        self, family_key: str, analysis_type: str, expected_intent: str
    ):
        """analysis_type válido se mapea al _intent legacy correcto."""
        result = _resolve_legacy_discriminator(family_key, analysis_type)
        assert result == expected_intent, (
            f"Family {family_key}, analysis_type {analysis_type!r}: "
            f"esperado {expected_intent!r}, obtuvo {result!r}"
        )


class TestResolveLegacyDiscriminatorNonAnalytic:
    """Tests de intents NO analíticos (que no están en _ANALYTIC_FAMILIES)."""

    def test_non_analytic_intent_returns_as_is(self):
        """Intent que no es analítico se retorna tal cual (sin resolver)."""
        # Estos no están en _ANALYTIC_FAMILIES, así que deben retornarse sin cambios
        non_analytic_intents = [
            "ingresar_venta",
            "ingresar_gasto",
            "consultar_estado_negocio",
            "actualizar_stock",
            "reclasificar_gasto",
            "intent_desconocido",
        ]

        for intent in non_analytic_intents:
            result = _resolve_legacy_discriminator(intent, None)
            assert result == intent, (
                f"Intent no-analítico {intent!r} con analysis_type=None "
                f"debe retornarse tal cual, obtuvo {result!r}"
            )

            result = _resolve_legacy_discriminator(intent, "algo")
            assert result == intent, (
                f"Intent no-analítico {intent!r} con analysis_type='algo' "
                f"debe retornarse tal cual, obtuvo {result!r}"
            )

    def test_unknown_intent_returns_as_is(self):
        """Intent desconocido se retorna tal cual."""
        unknown_intent = "esto_no_existe_en_catalogo"
        result = _resolve_legacy_discriminator(unknown_intent, None)
        assert result == unknown_intent

        result = _resolve_legacy_discriminator(unknown_intent, "algo_random")
        assert result == unknown_intent


class TestResolveLegacyDiscriminatorEdgeCases:
    """Tests de edge cases: tipos inesperados, None, strings vacíos, etc."""

    def test_none_analysis_type_all_families(self):
        """None como analysis_type para todas las familias retorna el default."""
        families = _ANALYTIC_FAMILIES
        for family_key, family_def in families.items():
            result = _resolve_legacy_discriminator(family_key, None)
            assert result == family_def["default"]

    def test_false_as_analysis_type_treated_as_falsy(self):
        """False/0/[] no son strings válidos, todos retornan default."""
        result = _resolve_legacy_discriminator("analizar_precios", False)
        assert result == "analizar_margenes_productos"

        result = _resolve_legacy_discriminator("analizar_stock", 0)
        assert result == "analizar_stock"

        result = _resolve_legacy_discriminator("analizar_ventas", [])
        assert result == "analizar_rentabilidad_ventas"

    def test_case_sensitive_analysis_type(self):
        """analysis_type matching es case-sensitive."""
        # "Margenes" (mayúscula) no debe matchear "margenes" (minúscula)
        result = _resolve_legacy_discriminator(
            "analizar_precios", "Margenes"
        )
        # Debe retornar el default porque "Margenes" ≠ "margenes"
        assert result == "analizar_margenes_productos"  # default, no el mapeo

    def test_whitespace_in_analysis_type(self):
        """analysis_type con espacios no matchea (case-sensitive, exact match)."""
        result = _resolve_legacy_discriminator(
            "analizar_stock", " quiebres "
        )
        # " quiebres " ≠ "quiebres", así que retorna default
        assert result == "analizar_stock"

    def test_very_long_analysis_type_string(self):
        """analysis_type muy largo no matchea, retorna default."""
        result = _resolve_legacy_discriminator(
            "analizar_precios",
            "analizar_margenes_productos_pero_con_mas_palabras_inutiles",
        )
        assert result == "analizar_margenes_productos"  # default


class TestResolveLegacyDiscriminatorConsistency:
    """Tests de consistencia y contrato general."""

    def test_resolve_always_returns_string(self):
        """_resolve_legacy_discriminator siempre retorna un string."""
        test_cases = [
            ("analizar_precios", None),
            ("analizar_precios", "margenes"),
            ("analizar_precios", "no_existe"),
            ("ingresar_venta", None),
            ("unknown_intent", "x"),
        ]
        for intent, analysis_type in test_cases:
            result = _resolve_legacy_discriminator(intent, analysis_type)
            assert isinstance(result, str), (
                f"Intent {intent!r}, analysis_type {analysis_type!r}: "
                f"debe retornar string, obtuvo {type(result).__name__}"
            )

    def test_all_mapped_intents_in_analytic_families_are_valid(self):
        """Todos los defaults son válidos; tipos mapeados también son válidos.

        Nota: El default NO necesariamente está en los mapped types (ej: ayuda_plataforma).
        Esto es válido — el default es el fallback cuando analysis_type es ausente o inválido.
        """
        families = _ANALYTIC_FAMILIES
        for family_key, family_def in families.items():
            mapped_intents = set(family_def["types"].values())
            default_intent = family_def["default"]

            # El default debe ser un string no vacío
            assert default_intent, (
                f"Family {family_key}: default no puede ser vacío/falsy"
            )

            # Todos los mapped intents deben ser strings no vacíos
            for type_key, mapped_intent in family_def["types"].items():
                assert isinstance(mapped_intent, str), (
                    f"Family {family_key}, type {type_key}: mapped intent no es string"
                )
                assert mapped_intent, (
                    f"Family {family_key}, type {type_key}: mapped intent es vacío"
                )

    def test_no_empty_strings_in_analytic_families(self):
        """Ningún default o mapping debe ser string vacío."""
        families = _ANALYTIC_FAMILIES
        for family_key, family_def in families.items():
            default = family_def["default"]
            assert default, f"Family {family_key}: default es vacío/falsy"

            for type_key, mapped_intent in family_def["types"].items():
                assert type_key, f"Family {family_key}: type_key vacío"
                assert mapped_intent, f"Family {family_key}, type {type_key}: mapped vacío"
