"""Piezas compartidas entre ``customer_import_service.py`` y
``supplier_import_service.py`` — extraídas del code-review de F-I(B).

Los dos servicios siguen siendo "espejos" deliberados (misma convención que
el resto del módulo: ``_validate_record``/``ImportResult``/``PreviewItem``
no se unifican, mismo criterio ya establecido antes de F-I(B)) — pero la
lógica de detección de duplicado-en-archivo y de persistencia de
``business_code`` es NUEVA de F-I(B) y no tenía motivo para vivir dos veces:
un fix futuro en una copia y no en la otra dejaría cliente y proveedor con
comportamientos distintos en silencio (exactamente lo que encontró el
code-review: la fila 1 conflictiva contaminaba ``seen_in_file`` en las
CUATRO copias del chequeo — 2 en preview, 2 en confirm).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.application.services.entity_code_service import (
    EntityIdentifierConflictError,
    record_identifier,
)
from app.application.services.identity_resolution import IdentityKey

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entity_code import EntityKind


def classify_duplicate_in_file(
    keys: list[IdentityKey], seen_in_file: dict[IdentityKey, int]
) -> int | None:
    """¿Alguna clave de esta fila ya la trajo OTRA fila de este archivo?

    Devuelve el índice de la primera fila que la trajo, o ``None`` si es la
    primera vez. Puro — no muta ``seen_in_file``, eso lo decide el caller
    (ver ``register_seen_keys``): el caller sólo debe registrar una fila
    DESPUÉS de confirmar que va a crear o actualizar algo — nunca antes,
    porque una fila que termina en ``needs_review``/``conflict`` no toca
    ninguna entidad y no debería poder "contaminar" la detección de
    duplicados para una fila posterior que sí es válida por su cuenta.
    """
    return next((seen_in_file[k] for k in keys if k in seen_in_file), None)


def register_seen_keys(
    keys: list[IdentityKey], seen_in_file: dict[IdentityKey, int], idx: int
) -> None:
    """Registra las claves de una fila que SÍ va a crear/actualizar algo.

    Llamar sólo después de que ``resolve_identity`` resolvió (nunca antes de
    saber si la fila termina en ``needs_review``/``conflict``) — ver el
    docstring de ``classify_duplicate_in_file``.
    """
    for k in keys:
        seen_in_file[k] = idx


async def persist_business_code(
    session: AsyncSession,
    tenant_id: UUID,
    entity_type: EntityKind,
    entity_id: UUID,
    record: dict[str, Any],
    uploaded_file_id: UUID | None,
) -> bool | None:
    """F-I(B): si la fila trae ``business_code``, lo persiste como
    ``EntityIdentifier`` — mismo patrón que ``_record_row_business_code`` de
    F-I(A) (``ingestion_import_service.py``), simplificado: acá el código es
    dato DIRECTO de la ficha, no un extra sobre un match resuelto por otra
    clave.

    Devuelve ``None`` si la fila no traía ``business_code`` (nada que
    hacer), ``True`` si se persistió, ``False`` si el código YA pertenece a
    OTRA entidad (``EntityIdentifierConflictError`` — de un import/fila
    anterior, no del mismo archivo: eso ya lo ataja ``classify_duplicate_
    in_file`` antes de llegar acá). El caller decide qué hacer con ``False``
    (contarlo — antes se descartaba en silencio, sin log ni contador, a
    diferencia de F-I(A)); nunca revierte la entidad: sus otros campos
    (documento, email, nombre) siguen siendo datos válidos.
    """
    raw_code = record.get("business_code")
    if raw_code is None or not str(raw_code).strip():
        return None
    try:
        await record_identifier(
            session,
            tenant_id,
            entity_type,
            entity_id,
            identifier_type="business_code",
            namespace="business",
            raw_value=str(raw_code),
            origin="business",
            source_upload_id=uploaded_file_id,
        )
    except EntityIdentifierConflictError:
        return False
    return True
