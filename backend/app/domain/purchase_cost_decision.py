"""F-H6.c — qué declaró el usuario sobre el costo de una hoja de compras, y si se puede honrar.

`purchase_cost.build_line_costs` sabe calcular; lo que no puede saber es **cuál de
sus modos quiso el usuario**. Esta decisión lo declara por hoja, con la misma
forma que `ShippingDecision` y `ColumnRiskDecision`: la pantalla ya sabe mandar
decisiones por contexto y el confirm ya sabe validarlas antes del lease.

Se valida ANTES de tomar el lease por el mismo motivo que las otras dos: una
decisión que no se puede honrar no se ignora en silencio, porque significa que el
usuario cree haber resuelto algo sobre sus costos que no va a pasar. Y un archivo
que va a rebotar nunca debería haber tomado el lease.

**Los defaults no cambian ningún número.** Los tres ejes arrancan en «no toques
nada», igual que el remito manual: distribuir, capitalizar o aplicar ajustes son
decisiones explícitas. Lo que NO puede ser es mudo — mapear una columna de
descuento y que no pase nada, sin decirlo, es el no-op silencioso que esta fase
viene a eliminar. Para eso está `hojas_que_necesitan_aviso`.

Módulo puro: sin sesión, sin ORM y sin leer el archivo. Recibe la decisión y el
mapeo efectivo, y devuelve mensajes en castellano.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.purchase_cost import (
    BASE_INCLUYE,
    COMPARTIDO_NO,
    COMPARTIDO_SUBTOTAL,
    COST_BASES,
    LINE_MODES,
    LINEA_AL_COSTO,
    LINEA_GASTO,
    SHARED_MODES,
)

#: Targets cuyo valor sólo entra al costo si el usuario declara una decisión.
#: Son los que F-M.7 agregó al catálogo y todavía nadie consumía.
TARGET_DESCUENTO = "discount"
TARGET_IMPUESTOS = "taxes"
TARGET_FLETE_LINEA = "shipping_cost_line"
TARGET_FLETE_COMPARTIDO = "shipping_cost"

#: Los que dependen de la BASE del costo: sin `monto_sin_ajustes` no se aplican.
TARGETS_DE_AJUSTE: frozenset[str] = frozenset({TARGET_DESCUENTO, TARGET_IMPUESTOS})

#: Etiqueta en castellano de cada target, para que el mensaje no muestre el nombre
#: técnico del campo. Espeja los labels de `CANONICAL_FIELDS["expense"]`.
_ETIQUETA: dict[str, str] = {
    TARGET_DESCUENTO: "Descuento de la línea",
    TARGET_IMPUESTOS: "Impuestos de la línea",
    TARGET_FLETE_LINEA: "Envío ya asignado a esta línea",
    TARGET_FLETE_COMPARTIDO: "Envío / flete",
}


@dataclass(frozen=True)
class PurchaseCostDecision:
    """Los tres ejes del costo de una hoja. Todo default no toca nada."""

    context_id: str
    #: ``monto_incluye`` (default): el monto de la fila ya trae descuento e
    #: impuestos adentro. ``monto_sin_ajustes``: es el bruto y hay que aplicarlos.
    base: str = BASE_INCLUYE
    #: Qué hacer con el envío que pertenece al comprobante ENTERO.
    shared_shipping: str = COMPARTIDO_NO
    #: Qué hacer con el envío que el archivo YA asignó a cada línea.
    line_shipping: str = LINEA_GASTO


def _targets(mapeo: dict[str, str]) -> set[str]:
    """Los campos canónicos a los que apunta el mapeo efectivo de una hoja."""
    return set(mapeo.values())


def validate_purchase_cost_decisions(
    decisiones: list[PurchaseCostDecision],
    mapeos_por_contexto: dict[str, dict[str, str]],
    etiqueta_de_hoja: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Motivos por los que una decisión no se puede honrar.

    Devuelve ``[(motivo_tecnico, mensaje_en_castellano)]`` — el primero para la
    traza de `pipeline_events`, el segundo para el 422 que ve la persona. Lista
    vacía = todas honrables.

    Rechaza tres cosas distintas, y las tres significan lo mismo para el usuario:
    declaró algo sobre sus costos que no va a pasar.

    1. Un modo que no existe. `build_line_costs` levanta `ValueError` ante uno
       desconocido en vez de caer a «no cobrar» en silencio; acá se corta antes,
       para que el error no aparezca a mitad de una importación.
    2. Una decisión sobre una hoja que no existe o que no mapea ninguna columna
       de costo.
    3. Una decisión que declara aplicar algo cuya columna no está mapeada:
       `monto_sin_ajustes` sin descuento ni impuestos, `al_costo` sin flete de
       línea, `por_subtotal` sin flete de comprobante. Honrarla sería un no-op, y
       el usuario ya declaró que esperaba un efecto.
    """
    etiquetas = etiqueta_de_hoja or {}
    errores: list[tuple[str, str]] = []

    def _hoja(cid: str) -> str:
        return etiquetas.get(cid, cid)

    for dec in decisiones:
        modos_invalidos: list[tuple[str, str]] = []
        if dec.base not in COST_BASES:
            modos_invalidos.append(
                (
                    "base_de_costo_desconocida",
                    f"La base del costo declarada para la hoja «{_hoja(dec.context_id)}» "
                    f"no existe: «{dec.base}».",
                )
            )
        if dec.shared_shipping not in SHARED_MODES:
            modos_invalidos.append(
                (
                    "modo_de_envio_compartido_desconocido",
                    f"El tratamiento del envío compartido declarado para la hoja "
                    f"«{_hoja(dec.context_id)}» no existe: «{dec.shared_shipping}».",
                )
            )
        if dec.line_shipping not in LINE_MODES:
            modos_invalidos.append(
                (
                    "modo_de_envio_de_linea_desconocido",
                    f"El tratamiento del envío de línea declarado para la hoja "
                    f"«{_hoja(dec.context_id)}» no existe: «{dec.line_shipping}».",
                )
            )
        if modos_invalidos:
            # Con un modo inválido no tiene sentido revisar la coherencia de esta
            # hoja: los demás mensajes serían ruido sobre un error que el usuario
            # ya tiene que corregir, y hablarían de una decisión que no se entiende.
            errores.extend(modos_invalidos)
            continue

        mapeo = mapeos_por_contexto.get(dec.context_id)
        if mapeo is None:
            errores.append(
                (
                    "decision_de_costo_sin_hoja",
                    f"La decisión sobre el costo apunta a la hoja "
                    f"«{_hoja(dec.context_id)}», que no está en el archivo.",
                )
            )
            continue

        presentes = _targets(mapeo)
        faltantes = _exigencias_incumplidas(dec, presentes)
        for motivo, target in faltantes:
            errores.append(
                (
                    motivo,
                    f"En la hoja «{_hoja(dec.context_id)}» se declaró usar "
                    f"«{_ETIQUETA[target]}», pero ninguna columna está mapeada a ese "
                    "campo. Mapeá la columna o sacá la decisión.",
                )
            )
    return errores


def _exigencias_incumplidas(
    dec: PurchaseCostDecision, presentes: set[str]
) -> list[tuple[str, str]]:
    """Qué columna exige cada decisión que se apartó del default."""
    faltan: list[tuple[str, str]] = []
    if dec.base != BASE_INCLUYE and not (TARGETS_DE_AJUSTE & presentes):
        # Basta con UNA de las dos: una planilla puede traer descuento y no IVA.
        faltan.append(("base_sin_columnas_de_ajuste", TARGET_DESCUENTO))
    if dec.line_shipping == LINEA_AL_COSTO and TARGET_FLETE_LINEA not in presentes:
        faltan.append(("flete_de_linea_sin_columna", TARGET_FLETE_LINEA))
    if dec.shared_shipping == COMPARTIDO_SUBTOTAL and TARGET_FLETE_COMPARTIDO not in presentes:
        faltan.append(("flete_compartido_sin_columna", TARGET_FLETE_COMPARTIDO))
    return faltan


def hojas_que_necesitan_aviso(
    decisiones: list[PurchaseCostDecision],
    mapeos_por_contexto: dict[str, dict[str, str]],
) -> dict[str, list[str]]:
    """Hojas donde una columna de costo mapeada NO va a mover ningún número.

    La contracara del default seguro. Sin decisión, el monto de la fila se toma
    como final —no se cambian números que el usuario no pidió cambiar— pero
    callarlo sería dejarlo creyendo que su descuento entró al costo.

    Devuelve ``{context_id: [targets ignorados]}``. No es un error: el import
    sigue, y el aviso viaja en la respuesta del confirm como ya hacen los de
    F-H6.b.
    """
    declarado = {d.context_id: d for d in decisiones}
    aviso: dict[str, list[str]] = {}
    for context_id, mapeo in mapeos_por_contexto.items():
        presentes = _targets(mapeo)
        dec = declarado.get(context_id)
        ignorados: list[str] = []
        if (TARGETS_DE_AJUSTE & presentes) and (dec is None or dec.base == BASE_INCLUYE):
            ignorados.extend(sorted(TARGETS_DE_AJUSTE & presentes))
        if TARGET_FLETE_LINEA in presentes and (
            dec is None or dec.line_shipping == LINEA_GASTO
        ):
            ignorados.append(TARGET_FLETE_LINEA)
        if ignorados:
            aviso[context_id] = ignorados
    return aviso


def texto_del_aviso(etiqueta_hoja: str, targets: list[str]) -> str:
    """El aviso, en castellano, listo para la respuesta del confirm."""
    campos = ", ".join(f"«{_ETIQUETA[t]}»" for t in targets)
    return (
        f"En la hoja «{etiqueta_hoja}» mapeaste {campos}, pero el monto de cada fila "
        "se tomó como final: esos valores no modificaron el costo. Si el monto es el "
        "bruto, declarálo para que se apliquen."
    )
