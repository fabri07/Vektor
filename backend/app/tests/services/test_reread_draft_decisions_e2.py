"""E2 — la relectura no puede perder decisiones que el usuario ya tomó.

Tres eslabones de la misma cadena, cada uno capaz de descartar la decisión por
su cuenta:

1. ``_draft_effective_mappings`` salía con ``(None, None)`` apenas
   ``column_mappings`` estaba vacío — sin llegar nunca a leer
   ``context_entities``. Reasignar una hoja a otra entidad, sin tocar ninguna
   columna, no llegaba al reimport.
2. Esa misma función FILTRABA los ``ignore``, devolviendo la columna al régimen
   heurístico. Su docstring lo justificaba con "es seguro pasar un mapeo parcial
   porque hay fallback heurístico", que es cierto para una columna sin revisar y
   falso para una decisión de excluirla: es H01 entrando por la relectura.
3. Aguas arriba, el endpoint envolvía TODO el bloque que persiste el borrador en
   un ``if body.column_mappings:``. Un body con sólo ``context_entity``
   respondía 200 y no guardaba nada — ni entidades, ni inclusión, ni decisiones
   de riesgo.

Los tres se prueban por separado a propósito: arreglar uno solo deja la cadena
igual de rota, y un test que los cubriera juntos no diría cuál falló.
"""

from __future__ import annotations

from typing import Any

from app.application.services.reread_service import _draft_effective_mappings


def _map(source: str, target: str, context_id: str | None = None) -> dict[str, Any]:
    fila: dict[str, Any] = {"source_column": source, "target_field": target}
    if context_id is not None:
        fila["context_id"] = context_id
    return fila


class TestElCambioDeEntidadSobrevive:
    def test_entidades_sin_ningun_mapeo_de_columnas(self) -> None:
        """El caso exacto: el usuario reasignó una hoja y no tocó una sola columna."""
        mappings, entidades = _draft_effective_mappings(
            {"context_entities": {"sheet:Hoja1": "expense"}}
        )
        assert mappings is None, "sin columnas mapeadas no hay mapeo que pasar"
        assert entidades == {"sheet:Hoja1": "expense"}, "la reasignación se perdía acá"

    def test_entidades_junto_a_mapeos(self) -> None:
        mappings, entidades = _draft_effective_mappings(
            {
                "column_mappings": [_map("Total", "amount", "sheet:Hoja1")],
                "context_entities": {"sheet:Hoja1": "expense"},
            }
        )
        assert mappings == {"sheet:Hoja1": {"Total": "amount"}}
        assert entidades == {"sheet:Hoja1": "expense"}

    def test_borrador_vacio_sigue_devolviendo_none(self) -> None:
        """Sin ninguna decisión, el caller tiene que caer al criterio de siempre."""
        assert _draft_effective_mappings({}) == (None, None)
        assert _draft_effective_mappings(None) == (None, None)


class TestElIgnoreLlegaAlReimport:
    def test_ignore_se_conserva_en_el_mapeo_efectivo(self) -> None:
        """Se pasa, no se filtra: es lo que hace que el importador saque la columna.

        Si acá se filtrara, el reimport la vería como "columna que el borrador no
        menciona" y la heurística volvería a leerla — que es el bug de H01.
        """
        mappings, _ = _draft_effective_mappings(
            {
                "column_mappings": [
                    _map("Total", "amount", "sheet:Hoja1"),
                    _map("Cantidad", "ignore", "sheet:Hoja1"),
                ]
            }
        )
        assert mappings == {"sheet:Hoja1": {"Total": "amount", "Cantidad": "ignore"}}

    def test_una_columna_sin_revisar_no_se_pasa(self) -> None:
        """``none`` sí se filtra: es una columna que nadie tocó, no una decisión.

        Bloquear la heurística ahí trabaría el flujo más común, que es aceptar el
        mapeo tal como vino.
        """
        mappings, _ = _draft_effective_mappings(
            {
                "column_mappings": [
                    _map("Total", "amount", "sheet:Hoja1"),
                    _map("Notas", "", "sheet:Hoja1"),
                ]
            }
        )
        assert mappings == {"sheet:Hoja1": {"Total": "amount"}}

    def test_una_columna_dropeada_por_riesgo_no_se_pasa(self) -> None:
        """``drop_column`` ya la sacó del summary: mapearla apuntaría a la nada.

        Es la diferencia con ``ignore``, que deja la columna en el summary y por
        eso necesita viajar hasta el importador.
        """
        mappings, _ = _draft_effective_mappings(
            {
                "column_mappings": [
                    _map("Total", "amount", "sheet:Hoja1"),
                    _map("Cantidad", "quantity", "sheet:Hoja1"),
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "sheet:Hoja1",
                        "source_column": "Cantidad",
                        "action": "drop_column",
                    }
                ],
            }
        )
        assert mappings == {"sheet:Hoja1": {"Total": "amount"}}

    def test_columnas_sin_contexto_caen_a_table(self) -> None:
        mappings, _ = _draft_effective_mappings(
            {"column_mappings": [_map("Cantidad", "ignore")]}
        )
        assert mappings == {"table": {"Cantidad": "ignore"}}
