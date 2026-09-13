"""Cambio 4 — comprobante de importación (docs/plans/conservacion-y-acceso-datos-negocio.md).

Por cada columna del archivo (mapeada o no), un veredicto: destino elegido,
resultado EFECTIVO (guardado/transformado/pendiente/excluido/mixto), motivo
legible y — SOLO cuando el importador puede acreditarlo — cuántas filas.

Regla dura: nunca deriva un conteo del contador global de la entidad
(``counts["productos"]`` etc.). Un conteo ausente se declara ausente
(``rows_affected=None``, "sin desglose acreditado"), nunca se inventa. Los
únicos conteos por columna que existen hoy son los que ``column_risk.py`` ya
calculó al aplicar las decisiones de riesgo (``routed_rows``) — no una
consulta nueva por fila.

Puro: no toca la sesión, no hace queries. Los callers (``confirm_file``,
``apply_reread``) le pasan lo que ya calcularon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Resultado = Literal["guardado", "transformado", "pendiente", "excluido", "mixto"]
TipoMotivo = Literal["user_decision", "system_rule"]

#: Campos que SIEMPRE pasan por resolución de convenio por columna (E4) antes
#: de guardarse — números y fechas. Un mapeo limpio a uno de estos targets es
#: "transformado", no "guardado": el valor que quedó en la base no es el texto
#: original de la celda.
_TARGETS_TRANSFORMADOS = frozenset(
    {
        "amount",
        "quantity",
        "unit_price",
        "unit_cost_ars",
        "sale_price_ars",
        "list_price_ars",
        "stock_units",
        "transaction_date",
        "acquired_at",
        "expiry_date",
        "birthday",
    }
)


@dataclass(frozen=True)
class ColumnReceipt:
    context_id: str
    context_label: str
    source_column: str
    target_field: str | None
    result: Resultado
    reason: str | None
    reason_kind: TipoMotivo | None
    #: None = "sin desglose acreditado" (nunca 0 por default: 0 es un valor real).
    rows_affected: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "context_label": self.context_label,
            "source_column": self.source_column,
            "target_field": self.target_field,
            "result": self.result,
            "reason": self.reason,
            "reason_kind": self.reason_kind,
            "rows_affected": self.rows_affected,
        }


def build_import_receipt(
    *,
    contexts: list[dict[str, Any]],
    effective_mapping: dict[str, dict[str, str]],
    dropped_columns: dict[str, list[str]] | None = None,
    routed_rows: dict[str, dict[int, dict[str, Any]]] | None = None,
    external_code_conflicts: int = 0,
) -> list[ColumnReceipt]:
    """Arma el comprobante columna por columna.

    ``contexts``: inventario COMPLETO de hojas/columnas — ``mapping_contexts``
    del summary (``context_id``, ``label``, ``headers``, ``row_count``), o un
    único pseudo-contexto ``"table"`` para un archivo plano. Se parte de ACÁ
    (no de ``effective_mapping``) para que una columna que el usuario nunca
    tocó también aparezca, con resultado "excluido, sin mapeo elegido" — el
    hallazgo de la revisión: "una columna sin mapear no puede desaparecer".

    ``external_code_conflicts``: contador GLOBAL del archivo (no hay desglose
    por hoja hoy). Si más de un contexto mapea a ``external_code``, no se le
    puede atribuir el conteo a ninguno en particular con certeza — se marca
    "mixto" con ``rows_affected=None`` en todos ellos en vez de inventar un
    reparto.
    """
    dropped_columns = dropped_columns or {}
    routed_rows = routed_rows or {}
    contexts_con_external_code = [
        ctx["context_id"]
        for ctx in contexts
        if "external_code" in effective_mapping.get(ctx["context_id"], {}).values()
    ]
    external_code_atribuible = len(contexts_con_external_code) == 1

    entries: list[ColumnReceipt] = []
    for ctx in contexts:
        cid = ctx["context_id"]
        label = str(ctx.get("label") or cid)
        headers = ctx.get("headers") or []
        mapping = effective_mapping.get(cid, {})
        dropped = set(dropped_columns.get(cid, []))
        routed = routed_rows.get(cid, {})
        row_total = ctx.get("row_count")

        for col in headers:
            target = mapping.get(col)

            if col in dropped:
                entries.append(
                    ColumnReceipt(
                        cid, label, col, target, "excluido",
                        "Se descartó por una decisión de riesgo (valores no "
                        "confiables en esta columna).",
                        "user_decision", None,
                    )
                )
                continue

            if target is None:
                entries.append(
                    ColumnReceipt(
                        cid, label, col, None, "excluido",
                        "No se mapeó a ningún campo.", "system_rule", None,
                    )
                )
                continue

            if target == "ignore":
                entries.append(
                    ColumnReceipt(
                        cid, label, col, None, "excluido",
                        'Se marcó como "Ignorar".', "user_decision", None,
                    )
                )
                continue

            afectadas = sum(1 for malas in routed.values() if col in malas)
            if afectadas:
                if row_total is not None and afectadas >= row_total:
                    entries.append(
                        ColumnReceipt(
                            cid, label, col, target, "pendiente",
                            f"Las {afectadas} fila(s) de esta hoja quedaron en "
                            "«Otros» por un valor riesgoso en esta columna.",
                            "user_decision", afectadas,
                        )
                    )
                else:
                    entries.append(
                        ColumnReceipt(
                            cid, label, col, target, "mixto",
                            f"{afectadas} fila(s) con un valor riesgoso en esta "
                            "columna se enviaron a «Otros»; el resto se guardó.",
                            "user_decision", afectadas,
                        )
                    )
                continue

            if target == "external_code" and external_code_conflicts:
                entries.append(
                    ColumnReceipt(
                        cid, label, col, target, "mixto",
                        f"{external_code_conflicts} valor(es) no se vincularon: "
                        "el código ya pertenece a otro producto del catálogo.",
                        "system_rule",
                        external_code_conflicts if external_code_atribuible else None,
                    )
                )
                continue

            result: Resultado = "transformado" if target in _TARGETS_TRANSFORMADOS else "guardado"
            entries.append(ColumnReceipt(cid, label, col, target, result, None, None, None))

    return entries
