"""F-N: propone separar "Nombre completo" en nombre/apellido — NUNCA lo hace
sola. Una fila real de ejemplo cerró el diseño anterior (heurística
"sin coma → primera palabra nombre, resto apellido" aplicada A CIEGAS,
siempre): "Juan Carlos Pérez" tiene nombre compuesto, y esa regla lo
convertía en nombre="Juan" / apellido="Carlos Pérez" sin que nadie lo
revisara.

Acá la regla sigue existiendo como PROPUESTA — se muestra en el preview
antes de confirmar, el usuario la acepta, la corrige, o la descarta — nunca
se persiste sola. Misma familia de la regla de no-invención que ya rige
fechas (F6-A2), filas sin monto (F-H4) y envío sin comprobante (F-H6.b):
entre elegir mal y no elegir, Véktor conserva el dato y pregunta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NameSplitStatus = Literal["proposed", "not_applicable", "ambiguous"]

_PARTICLES = frozenset({"de", "del", "van"})


@dataclass(frozen=True)
class NameSplitProposal:
    """Resultado de `propose_name_split` — nunca un hecho, siempre a revisar.

    - ``proposed``: hay una propuesta concreta (`first_name`/`last_name`).
    - ``not_applicable``: es una razón social (nunca se parte) o el nombre es
      una sola palabra (no hay dónde cortar).
    - ``ambiguous``: no hay evidencia suficiente para proponer nada con
      confianza — se muestra el nombre entero y se pregunta.
    """

    status: NameSplitStatus
    first_name: str | None
    last_name: str | None
    reason: str
    confidence_basis: str


def _split_by_comma(full_name: str) -> NameSplitProposal | None:
    if "," not in full_name:
        return None
    apellido, _, nombre = full_name.partition(",")
    apellido = apellido.strip()
    nombre = nombre.strip()
    if not apellido or not nombre:
        return NameSplitProposal(
            status="ambiguous",
            first_name=None,
            last_name=None,
            reason="Hay una coma pero falta el nombre o el apellido de un lado.",
            confidence_basis="coma sin los dos lados completos",
        )
    return NameSplitProposal(
        status="proposed",
        first_name=nombre,
        last_name=apellido,
        reason="Formato «Apellido, Nombre» — antes de la coma es el apellido.",
        confidence_basis="coma explícita",
    )


def _split_first_word(full_name: str, *, confidence_basis: str) -> NameSplitProposal:
    """Primera palabra = nombre, el resto = apellido — verbatim, así que una
    partícula (`de`, `del`, `van`) que empiece el resto queda pegada al
    apellido sin ningún manejo especial. El único caso raro es que el resto
    sea ÚNICAMENTE una partícula suelta (`"Juan de"`): ahí no hay apellido
    real detrás, así que no se propone nada."""
    words = full_name.split()
    if len(words) < 2:
        return NameSplitProposal(
            status="not_applicable",
            first_name=None,
            last_name=None,
            reason="Una sola palabra: no hay dónde partir nombre de apellido.",
            confidence_basis="una sola palabra",
        )
    rest = words[1:]
    if len(rest) == 1 and rest[0].lower() in _PARTICLES:
        return NameSplitProposal(
            status="ambiguous",
            first_name=None,
            last_name=None,
            reason="No se pudo separar un apellido real después del nombre.",
            confidence_basis="partícula sin apellido detrás",
        )
    return NameSplitProposal(
        status="proposed",
        first_name=words[0],
        last_name=" ".join(rest),
        reason="Sin coma: se propone la primera palabra como nombre — revisá antes de confirmar.",
        confidence_basis=confidence_basis,
    )


_RAZON_SOCIAL = NameSplitProposal(
    status="not_applicable",
    first_name=None,
    last_name=None,
    reason="Es una razón social (empresa) — el nombre queda entero.",
    confidence_basis="customer_type=company",
)

_SIN_EVIDENCIA = NameSplitProposal(
    status="ambiguous",
    first_name=None,
    last_name=None,
    reason="No hay evidencia suficiente de si es una persona o una empresa.",
    confidence_basis="sin customer_type ni doc_type=dni",
)


def propose_name_split(
    full_name: str,
    *,
    customer_type: str | None = None,
    doc_type: str | None = None,
) -> NameSplitProposal:
    """Propuesta de split para un CLIENTE. Precedencia explícita, no un `or`
    plano (la regla vieja `customer_type == person or doc_type == dni`
    partía una ficha marcada explícitamente como empresa con un DNI cargado
    por error):

    1. ``customer_type == "company"`` → nunca, es razón social.
    2. ``customer_type == "person"`` → elegible, confianza alta.
    3. ``customer_type`` desconocido + ``doc_type == "dni"`` → elegible,
       confianza baja (`doc_type == "cuit"` solo NO es evidencia de empresa
       — un monotributista persona física también tiene CUIT).
    4. Cualquier otra combinación → sin evidencia, `ambiguous`.

    Comparaciones case-insensitive: `_infer_doc_and_type`
    (`customer_extraction_service.py`) siempre escribe `doc_type` en
    minúscula (`"dni"`/`"cuit"`), pero la columna no tiene CHECK — normalizar
    acá evita que una fuente futura con otra casing rompa la propuesta en
    silencio.
    """
    full_name = (full_name or "").strip()
    if not full_name:
        return NameSplitProposal(
            status="not_applicable",
            first_name=None,
            last_name=None,
            reason="Sin nombre para partir.",
            confidence_basis="vacío",
        )
    ctype = (customer_type or "").strip().lower() or None
    dtype = (doc_type or "").strip().lower() or None
    if ctype == "company":
        return _RAZON_SOCIAL

    elegible = ctype == "person" or (ctype is None and dtype == "dni")
    if not elegible:
        return _SIN_EVIDENCIA

    por_coma = _split_by_comma(full_name)
    if por_coma is not None:
        return por_coma
    basis = "customer_type=person" if ctype == "person" else "doc_type=dni (tipo desconocido)"
    return _split_first_word(full_name, confidence_basis=basis)


def propose_supplier_name_split(full_name: str) -> NameSplitProposal:
    """Propuesta de split para un PROVEEDOR. `suppliers` no tiene columna de
    tipo (a diferencia de `customers.customer_type`) — la ausencia de CUIT
    no es evidencia de que sea persona (muchos proveedores empresa no traen
    CUIT en la planilla). Regla deliberadamente más conservadora: con coma
    propone igual que un cliente; sin coma NUNCA aplica la heurística de
    primera palabra, siempre `ambiguous` (o `not_applicable` si es una sola
    palabra)."""
    full_name = (full_name or "").strip()
    if not full_name:
        return NameSplitProposal(
            status="not_applicable",
            first_name=None,
            last_name=None,
            reason="Sin nombre para partir.",
            confidence_basis="vacío",
        )
    por_coma = _split_by_comma(full_name)
    if por_coma is not None:
        return por_coma
    if len(full_name.split()) < 2:
        return NameSplitProposal(
            status="not_applicable",
            first_name=None,
            last_name=None,
            reason="Una sola palabra: no hay dónde partir nombre de apellido.",
            confidence_basis="una sola palabra",
        )
    return NameSplitProposal(
        status="ambiguous",
        first_name=None,
        last_name=None,
        reason=(
            "Sin coma y sin un dato de tipo de ficha confiable para proveedores — "
            "Véktor no arriesga cuál palabra es el apellido."
        ),
        confidence_basis="proveedor sin coma, sin heurística de primera palabra",
    )
