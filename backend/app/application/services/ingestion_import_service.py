"""Shared insertion logic for confirmed parsed ingestion summaries."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.persistence.models.product import Product

from app.application.services import maintenance_lock_service, stock_service
from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.application.services.cash_service import normalize_payment_method
from app.application.services.file_parsing import FECHA_COLS as _FECHA_COLS
from app.application.services.file_parsing import GASTO_COLS as _GASTO_COLS
from app.application.services.file_parsing import VENTA_COLS as _VENTA_COLS
from app.application.services.identity_resolution import (
    IdentityKey,
    build_existing_index,
    record_keys,
    resolve_identity,
)
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_PURCHASE_IMPORT,
    SOURCE_RECEIPT,
    compute_source_row_hash,
    ensure_utc,
)
from app.application.services.product_identity import (
    MatchedBy,
    ProductIdentityConflictError,
    add_product_or_reuse,
)
from app.config.settings import get_settings
from app.domain.business_time import now_ar_naive
from app.domain.date_parsing import parse_business_date, parse_business_datetime
from app.domain.expense_categories import (
    classify_expense_with_vertical,
    infer_expense_type,
)
from app.domain.product_categories import normalize_product_category
from app.domain.text_norm import (
    normalize_barcode,
    normalize_brand,
    normalize_product_name,
    normalize_sku,
    normalize_text,
)
from app.domain.verticals import Vertical, parse_vertical
from app.observability.logger import get_logger

logger = get_logger(__name__)


class EmptyImportError(Exception):
    """Se confirmó un import con datos presentes pero no se insertó ninguna fila."""

    user_message = (
        "No se importó ninguna fila: no se detectaron automáticamente las "
        "columnas requeridas (fecha / monto / nombre) para el tipo confirmado. "
        "Mapeá las columnas manualmente o revisá el tipo de datos del archivo."
    )


def check_nonempty_import(
    counts: dict[str, Any],
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None,
    context_confirmed: dict[str, bool] | None = None,
    routed_to_others: int = 0,
) -> None:
    """Falla explícita ante inserción vacía con datos presentes.

    Si el archivo tenía filas y el usuario confirmó algún tipo pero NO se insertó
    nada (el fallback por keyword no encontró las columnas requeridas), lanza
    ``EmptyImportError`` en vez de dejar pasar un import silenciosamente vacío.
    Compartida por el endpoint de ingestión (→ 422) y el camino de chat
    (IMPORT_TABULAR_FILE → la pending action queda FAILED con mensaje visible).

    ``routed_to_others`` (F8c, default 0): cantidad de filas REALMENTE
    capturadas en "Otros" por una decisión ``route_affected_rows_to_others``
    de columna riesgosa (F8b) — conteo ya persistido (retorno de
    ``capture_column_risk_rows``), nunca planificado (invariante de
    no-invención). Si es > 0, el archivo no está "vacío": el usuario decidió
    explícitamente mandar esas filas a revisión manual, y esa decisión cuenta
    como manejo válido — no como pérdida silenciosa de datos (Minor 1 de F8b).
    El caller de chat (``pending_action_service``) no pasa este parámetro y
    conserva el comportamiento previo vía el default 0.
    """
    # NOTA: counts["otros"] NO cuenta como insertado. Si el usuario confirmó un
    # tipo y ese tipo importó 0 filas, hay que cortar con error (y el rollback
    # descarta también lo capturado a "Otros") para que pueda reintentar con
    # mapeo manual — si "otros" sumara, una hoja no clasificable taparía la
    # pérdida silenciosa de los datos confirmados.
    # F7c: clientes/proveedores (maestro) también cuentan como inserción — sin
    # esto, un archivo de SOLO clientes/proveedores que sí importó registros
    # disparaba este error igual (total_inserted quedaba en 0).
    total_inserted = (
        counts.get("ventas", 0)
        + counts.get("gastos", 0)
        + counts.get("productos", 0)
        + counts.get("clientes", 0)
        + counts.get("proveedores", 0)
    )
    had_rows = bool(summary.get("row_count")) or any(
        summary.get(k)
        for k in (
            "ventas_detectadas",
            "gastos_detectados",
            "stock_detectado",
            "otros_detectados",
            "clientes_detectados",
            "proveedores_detectados",
        )
    )
    confirmed_any = any((confirmed_fields or {}).values()) or any(
        (context_confirmed or {}).values()
    )
    if total_inserted == 0 and routed_to_others == 0 and had_rows and confirmed_any:
        logger.warning(
            "ingestion.import.zero_inserted",
            row_count=summary.get("row_count"),
            confirmed_fields=confirmed_fields,
        )
        raise EmptyImportError(EmptyImportError.user_message)


def _normalize_name(name: str) -> str:
    """Normaliza nombre de producto para comparación (matching de ventas/gastos/
    compras contra el catálogo).

    F2-T4 (unificación de resolución): delega en el normalizador canónico
    ``normalize_product_name`` (``text_norm``) — la MISMA clave que usa el motor
    de identidad del bucket de productos. Antes hacía solo ``lower`` + colapso de
    guiones y NO sacaba acentos, así que una venta "Cafe Molido" no matcheaba un
    producto de catálogo "Café Molido" pese a que el import de catálogo sí los
    trataba como el mismo. Con la delegación, los tres caminos comparten una única
    normalización (NFKD + sin diacríticos + casefold).
    """
    return normalize_product_name(name)


def _normalize_supplier_name(raw: str) -> str:
    """Normaliza nombre de proveedor para comparación: lower + espacios colapsados.

    A diferencia de ``_normalize_name`` (productos), NO colapsa guiones/underscores:
    el nombre comercial de un proveedor puede contener guiones legítimos
    ("Coca-Cola FEMSA") y no queremos fusionar identidades distintas.
    """
    return re.sub(r"\s+", " ", raw.strip().lower())


# ── Mejora A: captura de proveedor (find-or-create) en gastos importados ──────


async def _load_supplier_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """Carga los proveedores activos del tenant UNA vez (find-or-create en memoria).

    Mismo patrón que ``_load_product_index``: evita N queries (una por fila).
    Key = ``_normalize_supplier_name(name)`` → id. El PRIMER proveedor gana ante
    nombres normalizados duplicados (dedup; no se sobrescribe).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.supplier import (  # noqa: PLC0415
        SENTINEL_FLAG_KEY,
        Supplier,
        is_sentinel_value,
    )

    result = await session.execute(
        select(Supplier.id, Supplier.name, Supplier.custom_fields).where(
            Supplier.tenant_id == tenant_id,
            Supplier.deactivated_at.is_(None),
        )
    )
    index: dict[str, uuid.UUID] = {}
    for sid, sname, cfields in result.all():
        # El sentinela se cachea bajo su key dedicada (no por nombre): se resuelve
        # por flag, no por texto, y no debe matchear find-or-create por nombre.
        # ``is_sentinel_value`` acepta string "true" o booleano JSON (fuente única).
        if cfields and is_sentinel_value(cfields.get(SENTINEL_FLAG_KEY)):
            index.setdefault(_SENTINEL_INDEX_KEY, sid)
            continue
        norm = _normalize_supplier_name(sname or "")
        if norm and norm not in index:
            index[norm] = sid
    return index


async def _resolve_or_create_supplier(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    raw_name: Any,
    supplier_index: dict[str, uuid.UUID],
) -> tuple[uuid.UUID | None, str | None]:
    """Resuelve (o crea) el proveedor de una fila importada, devolviendo
    ``(supplier_id, supplier_name)``.

    Find-or-create contra el índice en memoria (cargado una vez por import):
      - celda vacía / "none" / "nan" → ``(None, None)``;
      - si el nombre normalizado ya está en el índice → reusa el proveedor;
      - si no, crea un ``Supplier`` nuevo, lo agrega a la sesión y hace
        ``flush`` para materializar su id (necesario para vincular el gasto y
        cachearlo) dentro de la misma transacción del import — mismo patrón de
        id-resolución que ``_ensure_product_for_purchase`` (que delega en
        ``build_incomplete_product`` con id explícito). Cachea el id nuevo para
        que filas posteriores del mismo proveedor lo reusen sin duplicar.
    """
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    clean = _clean_str(raw_name, 300)
    if not clean:
        return None, None
    norm = _normalize_supplier_name(clean)
    if not norm:
        return None, None
    hit = supplier_index.get(norm)
    if hit is not None:
        return hit, clean
    new_id = uuid.uuid4()
    session.add(Supplier(id=new_id, tenant_id=tenant_id, name=clean))
    # NO se hace flush: el id es explícito (``new_id``), así que no hace falta
    # materializar para vincular el gasto. Un flush por proveedor nuevo en un
    # import masivo era O(N) round-trips (flushea todo lo pendiente cada vez) —
    # un cuello real con libros de compras de muchos proveedores. El commit del
    # caller persiste Supplier y ExpenseEntry juntos (orden por FK).
    supplier_index[norm] = new_id
    return new_id, clean


# ── Sentinela "No identificado" (invariante: UNO por tenant) ──────────────────
# Compras de mercadería SIN proveedor informado se agrupan en un único proveedor
# sentinela por tenant ("No identificado"). Se distingue de los reales por el flag
# ``custom_fields["_sentinel"] == "true"`` y un índice único parcial en la DB
# garantiza el invariante. NO se aplica a OPEX (gastos operativos) sin proveedor:
# solo a compras de mercadería (fila con product_id, ``_is_merch_purchase``).
_SENTINEL_SUPPLIER_NAME = "No identificado"
# Índice PARCIAL (WHERE custom_fields->>'_sentinel' = 'true') → PG-only: en SQLite
# no existe y esta rama no se ejercita. Sin `columns` por eso mismo.
_SUPPLIER_SENTINEL_CONFLICT = unique_violation_classifier(
    "sentinel", constraint="uq_suppliers_sentinel_per_tenant"
)
# Key cacheada en supplier_index para el sentinela (el nombre podría editarse,
# pero el flag es el identificador canónico).
_SENTINEL_INDEX_KEY = "__sentinel__"


async def _resolve_or_create_sentinel_supplier(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    supplier_index: dict[str, uuid.UUID],
) -> uuid.UUID:
    """Find-or-create del proveedor sentinela "No identificado" del tenant.

    Busca por ``custom_fields["_sentinel"] == "true"`` (no solo por nombre: el
    usuario puede renombrarlo al completarlo, pero mientras tenga el flag sigue
    siendo el sentinela). Cachea el id en ``supplier_index`` bajo
    ``_SENTINEL_INDEX_KEY`` para que filas posteriores del mismo import lo reusen
    sin re-query. Maneja la concurrencia: si dos imports crean el sentinela a la
    vez, el índice único parcial dispara ``IntegrityError`` → re-query.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    cached = supplier_index.get(_SENTINEL_INDEX_KEY)
    if cached is not None:
        return cached

    async def _find() -> uuid.UUID | None:
        # ``.as_string()`` es cross-dialect (PG + SQLite de tests); ``.astext`` es
        # solo de PostgreSQL y rompe en SQLite.
        result = await session.execute(
            select(Supplier.id).where(
                Supplier.tenant_id == tenant_id,
                Supplier.deactivated_at.is_(None),
                Supplier.custom_fields["_sentinel"].as_string() == "true",
            )
        )
        return result.scalars().first()

    found = await _find()
    if found is not None:
        supplier_index[_SENTINEL_INDEX_KEY] = found
        return found

    new_id = uuid.uuid4()
    try:
        # `guarded_savepoint` aporta el ordenamiento (drenar fuera del try —crítico
        # acá: el import acumula decenas de objetos con `autoflush=False`— y agregar
        # DENTRO del savepoint) más el clasificador del índice del sentinela.
        # Ver services/_savepoint.py.
        async with guarded_savepoint(session, _SUPPLIER_SENTINEL_CONFLICT):
            session.add(
                Supplier(
                    id=new_id,
                    tenant_id=tenant_id,
                    name=_SENTINEL_SUPPLIER_NAME,
                    custom_fields={"_sentinel": "true"},
                )
            )
    except SavepointConflictError:
        existing = await _find()
        if existing is None:  # pragma: no cover — el índice garantiza que exista
            raise
        supplier_index[_SENTINEL_INDEX_KEY] = existing
        return existing
    supplier_index[_SENTINEL_INDEX_KEY] = new_id
    return new_id


def _audit_supplier_decision(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    decision_type: str,
    data: dict[str, Any],
) -> None:
    """Traza granular (agregada, no por fila) de las decisiones de proveedor del
    import: ``SUPPLIER_CREATED_FROM_PURCHASE`` / ``SUPPLIER_SENTINEL_CREATED`` /
    ``SUPPLIER_SKIPPED_FROM_CATALOG``. Insert-only, sin commit (entra en el batch
    del import). ``actor_user_id`` queda ``None``: el servicio no recibe el user.
    """
    from datetime import UTC  # noqa: PLC0415

    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415

    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={"record_type": "supplier", **data},
            triggered_by="ingestion:import",
            context={"source": "ingestion_import_service"},
            created_at=datetime.now(UTC),
        )
    )


# ── F7c: orden maestro→transacción + resolución de cliente/proveedor por fila ─
# 7a abrió los campos de referencia (``customer_dni``/``customer_cuit``/...,
# ``supplier_cuil``/...) en ``column_mapping_service``; 7b construyó el motor de
# identidad común (``identity_resolution``) + los import services de maestro
# (``customer_import_service``/``supplier_import_service``). Acá se cablean:
# los maestros se importan ANTES que cualquier venta/gasto (misma transacción
# del confirm), y cada fila transaccional resuelve su referencia contra el
# índice de identidad — nunca crea una entidad desde una fila.

_CUSTOMER_DOC_FIELDS: tuple[str, ...] = ("cuit", "dni")
_SUPPLIER_DOC_FIELDS: tuple[str, ...] = ("cuil",)

# Tokens de cliente "genérico" (venta de mostrador sin identificar). Comparados
# ya normalizados (``normalize_text``: sin acentos, sin mayúsculas, sin
# voseo/espacios de más) — "Público", "PUBLICO", "público " matchean igual.
_ANONYMOUS_CUSTOMER_TOKENS: frozenset[str] = frozenset(
    normalize_text(t)
    for t in (
        "consumidor final",
        "consumidor",
        "mostrador",
        "local",
        "publico",
        "publico general",
        "varios",
        "sin nombre",
        "sin datos",
        "cliente ocasional",
        "n/a",
        "na",
    )
)


@dataclass
class RowReferenceResolution:
    """Resultado de clasificar la referencia de UNA fila transaccional (venta→
    cliente, compra→proveedor) contra el índice de identidad ya armado. Nunca
    crea — solo matchea o deriva a revisión (ver ``_classify_row_reference``).
    """

    outcome: str  # "matched" | "anonymous" | "unresolved"
    entity: Any | None = None
    raw_value: str | None = None


def _classify_row_reference(
    record: dict[str, Any],
    *,
    doc_fields: tuple[str, ...],
    existing_index: dict[IdentityKey, Any],
    anonymous_name_tokens: frozenset[str] | None = None,
) -> RowReferenceResolution:
    """Clasifica la referencia de una fila transaccional en la semántica de 3
    vías de F7c, sobre el motor F7b (``identity_resolution.resolve_identity``):

    - ``matched``: una clave fuerte (documento > email > teléfono) matchea una
      única entidad existente del índice.
    - ``anonymous``: sin ninguna referencia (fila de mostrador — el caso normal,
      SIN warning) o el único dato es un nombre "genérico"
      (``anonymous_name_tokens``, ej. "Consumidor final").
    - ``unresolved``: hay una referencia (documento/email/teléfono, o un nombre
      real sin clave fuerte) pero no matchea, o matchea a más de una entidad —
      se marca para revisión. NUNCA crea.
    """
    name_raw = record.get("name")
    name_norm = normalize_text(str(name_raw)) if name_raw else ""
    if anonymous_name_tokens is not None and name_norm and name_norm in anonymous_name_tokens:
        return RowReferenceResolution(outcome="anonymous")

    keys = record_keys(record, doc_fields=doc_fields)
    if not keys and not name_norm:
        return RowReferenceResolution(outcome="anonymous")

    resolution = resolve_identity(keys, existing_index)
    if resolution.outcome == "matched":
        assert resolution.entity is not None  # invariante de "matched"
        return RowReferenceResolution(outcome="matched", entity=resolution.entity)

    # unresolved: needs_review (solo nombre débil), none (clave sin match) o
    # conflict (clave ambigua) — todas se tratan igual acá: no identifican sin
    # ambigüedad, van al sentinela con traza para revisión humana.
    raw: Any = name_raw
    if raw is None:
        for f in doc_fields:
            if record.get(f):
                raw = record[f]
                break
        else:
            raw = record.get("email") or record.get("phone")
    return RowReferenceResolution(
        outcome="unresolved", raw_value=str(raw) if raw is not None else None
    )


# F7d: taxonomía reconciliada de contadores de resolución por fila. Única fuente
# de las 3 claves por dominio — evita que counts termine con dos formas de contar
# lo mismo (el gap que 7d vino a cerrar: 7c dejó "clientes_sin_identificar", acá
# se reemplaza por "ventas_cliente_no_resuelto", mismo criterio para compras).
_REFERENCE_OUTCOME_SUFFIX = {
    "matched": "identificado",
    "anonymous": "anonimo",
    "unresolved": "no_resuelto",
}


def _bump_reference_counts(counts: dict[str, Any], prefix: str, outcome: str) -> None:
    """Suma 1 a ``{prefix}_{identificado|anonimo|no_resuelto}`` según ``outcome``.

    ``anonimo`` (sin referencia — venta de mostrador, compra sin proveedor
    informado) NUNCA amerita revisión humana — ver la regla de warnings en
    ``api/v1/ingestion.py`` (solo ``no_resuelto`` avisa).
    """
    key = f"{prefix}_{_REFERENCE_OUTCOME_SUFFIX[outcome]}"
    counts[key] = counts.get(key, 0) + 1


def _rows_for_context(bucket: list[dict[str, Any]], ctx_id: str) -> list[dict[str, Any]]:
    """Filtra un bucket de filas por ``__context__`` (multi-hoja). Si las filas no
    llevan el marcador (archivo de un solo contexto, ej. un CSV de clientes
    suelto), el bucket entero pertenece a ese único contexto."""
    if not bucket:
        return []
    if "__context__" not in bucket[0]:
        return bucket
    return [r for r in bucket if r.get("__context__") == ctx_id]


def _customer_reference_record(row: dict[str, Any], cols: dict[str, str]) -> dict[str, Any]:
    """Arma el record de referencia de cliente de una fila de venta desde las
    columnas ``customer_*`` mapeadas (F7a). Sin mapeo explícito para un campo,
    queda ``None`` — la referencia es opt-in, no se adivina por keyword (a
    diferencia de monto/fecha, que sí tienen fallback): sin columna de cliente
    mapeada, la fila es ``anonymous`` (venta de mostrador), no ``unresolved``.
    """
    return {
        "cuit": row.get(cols["customer_cuit"]) if cols.get("customer_cuit") else None,
        "dni": row.get(cols["customer_dni"]) if cols.get("customer_dni") else None,
        "email": row.get(cols["customer_email"]) if cols.get("customer_email") else None,
        "phone": row.get(cols["customer_phone"]) if cols.get("customer_phone") else None,
        "name": row.get(cols["customer_name"]) if cols.get("customer_name") else None,
    }


def _supplier_reference_record(
    row: dict[str, Any], cols: dict[str, str], name_raw: Any
) -> dict[str, Any]:
    """Arma el record de referencia de proveedor de una fila de compra desde las
    columnas ``supplier_*`` mapeadas (F7a). ``name_raw`` es el valor de nombre ya
    resuelto por el caller (mapeo explícito o keyword ``_PROVEEDOR_COLS`` — mismo
    dato que usa hoy ``_resolve_or_create_supplier``, no se duplica esa detección).
    """
    return {
        "cuil": row.get(cols["supplier_cuil"]) if cols.get("supplier_cuil") else None,
        "email": row.get(cols["supplier_email"]) if cols.get("supplier_email") else None,
        "phone": row.get(cols["supplier_phone"]) if cols.get("supplier_phone") else None,
        "name": name_raw,
    }


async def _load_customer_identity_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[IdentityKey, Any]:
    """Índice de identidad de clientes (documento→email→teléfono) para resolver
    referencias de fila en ventas. Reusa el motor F7b; excluye el sentinela
    "Local" y los desactivados (``CustomerRepository.list_for_dedup``)."""
    from app.persistence.repositories.customer_repository import (  # noqa: PLC0415
        CustomerRepository,
    )

    existing = await CustomerRepository(session).list_for_dedup(tenant_id)
    return build_existing_index(
        existing,
        to_record=lambda c: {"cuit": c.cuit, "dni": c.dni, "email": c.email, "phone": c.phone},
        doc_fields=_CUSTOMER_DOC_FIELDS,
    )


async def _load_supplier_identity_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[IdentityKey, Any]:
    """Índice de identidad de proveedores (CUIL→email→teléfono). Solo se carga en
    modo ``link_only`` (ver ``SUPPLIER_REFERENCE_CREATION_MODE``) — en "legacy" no
    hace falta, el comportamiento de compras no cambia."""
    from app.persistence.repositories.supplier_repository import (  # noqa: PLC0415
        SupplierRepository,
    )

    existing = await SupplierRepository(session).list_for_dedup(tenant_id)
    return build_existing_index(
        existing,
        to_record=lambda s: {"cuil": s.cuil, "email": s.email, "phone": s.phone},
        doc_fields=_SUPPLIER_DOC_FIELDS,
    )


async def _import_master_entities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool],
    context_mappings: dict[str, dict[str, str]] | None,
    context_confirmed: dict[str, bool] | None,
    column_mappings: dict[str, str] | None,
    counts: dict[str, Any],
) -> None:
    """Paso 1/2 del orden maestro→transacción: importa clientes y proveedores
    ANTES de cualquier venta/gasto, reusando los import services de F7b
    (``customer_import_service.apply_import`` / ``supplier_import_service.
    apply_import``). Corre dentro de la MISMA transacción del confirm — sin
    commit intermedio, así que si algo posterior falla, el rollback integral
    también deshace estas altas/actualizaciones.

    Recorre los contextos ``entity_type in ("customer", "supplier")`` de
    ``mapping_contexts`` (siempre presente — ``file_parsing`` arma al menos el
    contexto "table" para un archivo de una sola hoja). Sin mapeo explícito
    (``context_mappings``/``column_mappings``) para esa hoja, no se importa esta
    corrida: no hay forma de saber qué columna es el DNI/CUIT/email de cada
    fila sin adivinar el shape de los datos.
    """
    from app.application.services import (  # noqa: PLC0415
        customer_import_service,
        supplier_import_service,
    )
    from app.application.services.column_mapping_service import CANONICAL_FIELDS  # noqa: PLC0415
    from app.persistence.repositories.customer_repository import (  # noqa: PLC0415
        CustomerRepository,
    )
    from app.persistence.repositories.supplier_repository import (  # noqa: PLC0415
        SupplierRepository,
    )

    for ctx in summary.get("mapping_contexts") or []:
        entity = ctx.get("entity_type")
        ctx_id = str(ctx.get("context_id") or "")
        if entity == "customer":
            confirm_key = "clientes"
            bucket_key = "clientes_detectados"
        elif entity == "supplier":
            confirm_key = "proveedores"
            bucket_key = "proveedores_detectados"
        else:
            continue

        # Inclusión: por contexto si vino context_confirmed; si no, por tipo
        # (legacy) — mismo criterio que el dispatch de ventas/gastos/productos.
        if context_confirmed:
            if not context_confirmed.get(ctx_id):
                continue
        elif not confirmed_fields.get(confirm_key):
            continue

        rows = _rows_for_context(summary.get(bucket_key) or [], ctx_id)
        if not rows:
            continue
        mapping = (context_mappings or {}).get(ctx_id) or (
            column_mappings if ctx_id == "table" else None
        ) or {}
        target_to_col, _ = _resolve_target_cols(mapping)
        if not target_to_col:
            continue  # sin mapeo explícito: no se adivina el shape de la fila

        if entity == "customer":
            customer_records = [
                {
                    f: row.get(target_to_col[f])
                    for f in CANONICAL_FIELDS["customer"]
                    if f in target_to_col
                }
                for row in rows
            ]
            cust_result = await customer_import_service.apply_import(
                CustomerRepository(session), tenant_id, customer_records
            )
            counts["clientes"] = (
                counts.get("clientes", 0)
                + len(cust_result.created_ids)
                + len(cust_result.updated_ids)
            )
            counts["clientes_creados"] = (
                counts.get("clientes_creados", 0) + len(cust_result.created_ids)
            )
            counts["clientes_actualizados"] = (
                counts.get("clientes_actualizados", 0) + len(cust_result.updated_ids)
            )
            counts["clientes_needs_review"] = (
                counts.get("clientes_needs_review", 0) + cust_result.needs_review
            )
            counts["clientes_invalidos"] = (
                counts.get("clientes_invalidos", 0) + cust_result.invalid
            )
            counts["clientes_creados_ids"] = counts.get("clientes_creados_ids", []) + [
                str(i) for i in cust_result.created_ids
            ]
            counts["clientes_actualizados_ids"] = counts.get(
                "clientes_actualizados_ids", []
            ) + [str(i) for i in cust_result.updated_ids]
        else:
            supplier_records = [
                {
                    f: row.get(target_to_col[f])
                    for f in CANONICAL_FIELDS["supplier"]
                    if f in target_to_col
                }
                for row in rows
            ]
            sup_result = await supplier_import_service.apply_import(
                SupplierRepository(session), tenant_id, supplier_records
            )
            counts["proveedores"] = (
                counts.get("proveedores", 0)
                + len(sup_result.created_ids)
                + len(sup_result.updated_ids)
            )
            counts["proveedores_creados"] = (
                counts.get("proveedores_creados", 0) + len(sup_result.created_ids)
            )
            counts["proveedores_actualizados"] = (
                counts.get("proveedores_actualizados", 0) + len(sup_result.updated_ids)
            )
            counts["proveedores_needs_review"] = (
                counts.get("proveedores_needs_review", 0) + sup_result.needs_review
            )
            counts["proveedores_invalidos"] = (
                counts.get("proveedores_invalidos", 0) + sup_result.invalid
            )
            counts["proveedores_creados_ids"] = counts.get(
                "proveedores_creados_ids", []
            ) + [str(i) for i in sup_result.created_ids]
            counts["proveedores_actualizados_ids"] = counts.get(
                "proveedores_actualizados_ids", []
            ) + [str(i) for i in sup_result.updated_ids]


# ── B1: idempotencia de imports (anti re-subida del mismo archivo) ────────────

_IMPORT_ROW_ACTION = "IMPORT_ROW"

# F8b (Task 5): clave reservada en ``UnclassifiedRecord.row_data`` que correlaciona
# la captura de riesgo con su fila de origen ``(context_id, row_index)``. La usa la
# relectura (``reread_service``) para resolver el "Otros" previo cuando la fila
# aparece corregida. Es PII-free (solo id de contexto + índice), va bajo prefijo
# ``__`` como ``__context__`` (interno, no dato de negocio) y ``/otros`` la oculta
# del render. Valor = JSON string (``json.dumps({context_id, row_index})``).
RISK_REF_KEY = "__risk_ref__"

_ROW_FINGERPRINT_CONFLICT = unique_violation_classifier(
    "fingerprint",
    constraint="uq_operation_fingerprints_tenant_fp",
    columns=("operation_fingerprints.tenant_id", "operation_fingerprints.fingerprint"),
)

# Tratamiento del stock de un archivo de catálogo/lista (lo elige el usuario en el
# confirm). "opening_balance" = saldo de apertura (activo que ya tenía, sin COGS/caja);
# "purchase" = compra (COGS + baja de caja). Default conservador: apertura (no distorsiona
# la caja registrando un egreso que quizá nunca ocurrió hoy).
STOCK_TREATMENT_OPENING_BALANCE = "opening_balance"
STOCK_TREATMENT_PURCHASE = "purchase"
_VALID_STOCK_TREATMENTS = frozenset({STOCK_TREATMENT_OPENING_BALANCE, STOCK_TREATMENT_PURCHASE})


async def _load_import_fingerprints(
    session: AsyncSession, tenant_id: uuid.UUID
) -> set[str]:
    """Precarga (una sola query) las huellas de filas de import del tenant.

    Evita el N+1: en vez de un ``SELECT`` por fila en ``_import_row_seen`` y un
    ``begin_nested()`` por fila en ``_register_import_row_fingerprint`` (miles de
    round-trips a la DB en archivos grandes), se carga el set una vez y la
    deduplicación corre en memoria. El set es el estado de ``operation_fingerprints``
    al inicio de la corrida; los anclas son únicos por (archivo, contexto, índice),
    así que dentro de una misma corrida basta con ir agregándolos al set.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.memory import OperationFingerprint  # noqa: PLC0415

    result = await session.execute(
        select(OperationFingerprint.fingerprint).where(
            OperationFingerprint.tenant_id == tenant_id,
            OperationFingerprint.action_type == _IMPORT_ROW_ACTION,
        )
    )
    return set(result.scalars().all())


async def _persist_import_fingerprints(
    session: AsyncSession, tenant_id: uuid.UUID, fingerprints: set[str]
) -> None:
    """Persiste en LOTE las huellas nuevas del camino batch con ``ON CONFLICT DO
    NOTHING`` — idempotente y seguro ante corridas concurrentes (una corrida que
    insertó la misma huella entre el preload y este flush NO aborta la transacción;
    simplemente se ignora). Reemplaza el ``begin_nested()`` por fila.

    El ``id`` se incluye explícito porque el ``default=uuid.uuid4`` del modelo es
    Python-side y un INSERT por Core no lo aplica solo. ``executed_at`` tiene
    server_default, así que no hace falta.
    """
    if not fingerprints:
        return
    from app.persistence.models.memory import OperationFingerprint  # noqa: PLC0415

    rows = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "fingerprint": fp,
            "action_type": _IMPORT_ROW_ACTION,
        }
        for fp in fingerprints
    ]
    bind = session.bind
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        await session.execute(
            _pg_insert(OperationFingerprint)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tenant_id", "fingerprint"])
        )
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        await session.execute(
            _sqlite_insert(OperationFingerprint)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tenant_id", "fingerprint"])
        )
    else:  # pragma: no cover — fallback genérico (sin garantía de idempotencia)
        from sqlalchemy import insert as _insert  # noqa: PLC0415

        await session.execute(_insert(OperationFingerprint).values(rows))


async def _register_import_row_fingerprint(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    anchor: str,
    seen: set[str] | None = None,
) -> bool:
    """Registra (idempotentemente) la huella de una fila importada.

    El ancla identifica unívocamente la fila dentro de su archivo/contexto
    (``sha256("{tenant}:IMPORT_ROW:{uploaded_file_id}:{context_id}:{row_index}")``
    o, para el bulk de "Otros", anclado en ``UnclassifiedRecord.id``).

    Con ``seen`` precargado (camino batch): la deduplicación es en memoria — si la
    huella ya está en el set, devolvemos ``True`` (skip); si no, se agrega SOLO al
    set y la persistencia se hace en lote al final vía ``_persist_import_fingerprints``
    (``INSERT ... ON CONFLICT DO NOTHING``), que es idempotente y seguro ante
    corridas concurrentes — sin un savepoint ni un INSERT por fila.
    Sin ``seen`` (camino legacy): se inserta bajo ``begin_nested()`` y el
    ``IntegrityError`` del savepoint indica que ya existía. Mismo patrón que
    ``api/v1/agent.py:_register_operation_fingerprint``.

    Devuelve ``True`` si la fila ya estaba registrada (skip), ``False`` si es
    nueva (procesar).
    """

    from app.persistence.models.memory import OperationFingerprint  # noqa: PLC0415

    fingerprint = hashlib.sha256(anchor.encode()).hexdigest()
    if seen is not None:
        if fingerprint in seen:
            return True
        # Solo se trackea en memoria; se persiste en lote al final (idempotente).
        seen.add(fingerprint)
        return False
    # `guarded_savepoint`: ordenamiento + clasificador. Sin el clasificador, una FK
    # rota o un NOT NULL del propio fingerprint se leería como "fila ya importada" y
    # la fila se SALTEARÍA en silencio. Ver services/_savepoint.py.
    try:
        async with guarded_savepoint(session, _ROW_FINGERPRINT_CONFLICT):
            session.add(
                OperationFingerprint(
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    action_type=_IMPORT_ROW_ACTION,
                )
            )
    except SavepointConflictError:
        return True
    return False


async def _import_row_seen(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    anchor: str,
    seen: set[str] | None = None,
) -> bool:
    """¿La fila (por su ancla) ya fue importada en una corrida previa?

    Con ``seen`` precargado: chequeo en memoria (sin round-trip). Sin ``seen``:
    ``SELECT`` sobre ``operation_fingerprints`` por ``(tenant_id, fingerprint)``.
    En ambos casos es READ-ONLY: la huella se registra recién DESPUÉS, con
    ``_register_import_row_fingerprint``, y solo si la fila produjo al menos un
    registro (venta/gasto). Así una fila inválida/mal mapeada que no insertó nada
    no queda marcada como importada y puede reintentarse corregida.

    Devuelve ``True`` si la huella ya existe (skip), ``False`` si es nueva.
    """
    fingerprint = hashlib.sha256(anchor.encode()).hexdigest()
    if seen is not None:
        return fingerprint in seen

    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.memory import OperationFingerprint  # noqa: PLC0415

    result = await session.execute(
        select(OperationFingerprint.id).where(
            OperationFingerprint.tenant_id == tenant_id,
            OperationFingerprint.fingerprint == fingerprint,
        )
    )
    return result.first() is not None


def _import_row_anchor(
    tenant_id: uuid.UUID,
    uploaded_file_id: uuid.UUID | None,
    context_id: str | None,
    row_index: int,
) -> str:
    """Ancla canónica de una fila de import (archivo + contexto + índice)."""
    return (
        f"{tenant_id}:{_IMPORT_ROW_ACTION}:"
        f"{uploaded_file_id or ''}:{context_id or ''}:{row_index}"
    )


def _risk_row_anchor(
    tenant_id: uuid.UUID,
    uploaded_file_id: uuid.UUID | None,
    context_id: str,
    row_index: int,
) -> str:
    """Ancla de la huella de captura de riesgo (F8b, invariante 6) — namespace
    PROPIO ``risk``, NUNCA el texto de ``_import_row_anchor`` (``IMPORT_ROW``,
    el ancla de venta/gasto). Así una fila puede tener SIMULTÁNEAMENTE su
    ancla de import normal (si se importó con éxito) y su ancla de riesgo (si
    además quedó con columnas riesgosas ruteadas a "Otros") sin que una huella
    bloquee a la otra. Incluye ``tenant_id``/``uploaded_file_id`` (a diferencia
    del `risk:{context_id}:{row_index}` literal del brief) para que dos
    archivos distintos del mismo tenant que reusan el mismo ``context_id``
    sintético (p.ej. ``"table"`` en single-sheet) no colisionen entre sí.
    """
    return f"{tenant_id}:risk:{uploaded_file_id or ''}:{context_id}:{row_index}"


def _source_row_ref(anchor: str | None) -> str | None:
    """Mejora D: ref estable de 64 chars para ``source_row_ref`` desde el ancla.

    ``source_row_ref`` es ``VARCHAR(64)`` y el ancla cruda (tenant:file:ctx:idx)
    excede ese largo, así que se persiste el ``sha256`` hex (exactamente 64
    chars) — mismo derivado determinístico que la huella de idempotencia, así la
    relectura puede recomputarlo desde el mismo ancla para reconciliar la fila.
    """
    if anchor is None:
        return None
    return hashlib.sha256(anchor.encode()).hexdigest()


def _capture_unclassified(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    headers: list[str] | None,
    source: str,
    uploaded_file_id: uuid.UUID | None,
    context_label: str | None = None,
    suggested_entity: str | None = None,
    match_candidates: list[dict[str, Any]] | None = None,
) -> int:
    """FASE F: persiste filas no clasificadas en la bandeja "Otros".

    Nada se descarta en silencio: lo que no se pudo (o no se quiso) clasificar
    como venta/gasto/producto queda en ``unclassified_records`` con estado
    PENDING para que el tenant lo importe o descarte desde /otros.

    F2-T2: ``match_candidates`` (solo para filas de producto ambiguas o en
    conflicto de identidad) — forma ``{id, matched_by, name, sku, barcode}``.
    """
    from app.persistence.models.unclassified_record import (  # noqa: PLC0415
        UnclassifiedRecord,
    )

    count = 0
    for row in rows:
        row_data = {k: v for k, v in row.items() if k != "__context__"}
        if not row_data:
            continue
        session.add(
            UnclassifiedRecord(
                tenant_id=tenant_id,
                uploaded_file_id=uploaded_file_id,
                source=source,
                context_label=(context_label or None),
                headers=list(headers) if headers else None,
                row_data={k: ("" if v is None else str(v)) for k, v in row_data.items()},
                suggested_entity=suggested_entity,
                match_candidates=match_candidates,
            )
        )
        count += 1
    return count


async def _capture_column_risk_rows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    uploaded_file_id: uuid.UUID | None,
    context_id: str,
    entity_type: str,
    affected_rows: dict[int, dict[str, Any]],
    source: str = "ingestion",
) -> int:
    """F8b (Task 3): captura en "Otros" las filas de un contexto afectadas por
    una decisión ``route_affected_rows_to_others`` — UNA ``UnclassifiedRecord``
    por fila, combinando TODOS los campos problemáticos de esa fila.

    ``affected_rows`` es ``{row_index: {campo_problemático: valor_crudo}}`` YA
    agrupado por fila: si dos columnas riesgosas distintas del mismo contexto
    afectan la misma fila, el CALLER (Task 4) combina ambas en un solo dict
    antes de invocar esta primitiva — acá no se vuelve a agrupar entre
    columnas, solo se persiste 1:1 por ``row_index``.

    Idempotencia — namespace de huella PROPIO (invariante 6, ``_risk_row_anchor``):
    nunca el ancla ``IMPORT_ROW`` de venta/gasto. La huella se registra
    DESPUÉS de persistir la captura (``_capture_unclassified`` primero,
    ``_register_import_row_fingerprint`` después) — si la fila combinada
    queda vacía (nada que capturar), NO se registra huella, para que un
    reintento con datos corregidos no quede bloqueado por una huella
    "fantasma" que nunca capturó nada (mismo principio que
    ``_import_row_seen``/``_register_import_row_fingerprint`` para el ancla de
    import normal). Un reintento con la MISMA fila (mismo contexto + índice)
    hace skip sin duplicar.

    ``context_label`` incluye SOLO nombres de columna (nunca valores) —
    invariante 7, sin PII.

    Devuelve la cantidad de ``UnclassifiedRecord`` NUEVAS creadas (para sumar
    a ``counts["otros"]`` en el caller).
    """
    created = 0
    for row_index, row_data in affected_rows.items():
        # Fila combinada vacía (nada que capturar): se saltea SIN registrar huella,
        # para que un reintento con datos corregidos no quede bloqueado por una
        # huella "fantasma" (mismo principio que ``_import_row_seen``).
        if not row_data:
            continue
        anchor = _risk_row_anchor(tenant_id, uploaded_file_id, context_id, row_index)
        if await _import_row_seen(session, tenant_id, anchor):
            continue
        context_label = (
            f"Columna riesgosa ({entity_type}, contexto '{context_id}'): "
            + ", ".join(sorted(row_data))
        )[:200]
        # Correlación PII-free (Task 5): (context_id, row_index) → este registro,
        # para que la relectura pueda resolver el "Otros" al aparecer corregida la
        # fila. Va aparte de las columnas problemáticas (headers/label la excluyen;
        # ``/otros`` oculta las keys ``__``).
        _payload = {
            **row_data,
            RISK_REF_KEY: json.dumps(
                {"context_id": context_id, "row_index": row_index}
            ),
        }
        captured = _capture_unclassified(
            session,
            tenant_id,
            rows=[_payload],
            headers=(sorted(row_data) or None),
            source=source,
            uploaded_file_id=uploaded_file_id,
            context_label=context_label,
            suggested_entity=entity_type,
        )
        if captured == 0:
            # Fila combinada vacía (nada que capturar): no hay nada persistido,
            # así que NO se registra la huella — ver docstring.
            continue
        await _register_import_row_fingerprint(session, tenant_id, anchor)
        created += captured
    return created


async def _load_tenant_vertical(session: AsyncSession, tenant_id: uuid.UUID) -> Vertical:
    """Vertical del tenant para normalizar categorías de producto (1 query por import).

    Sin fallback: importar con el catálogo de otro rubro clasifica mal cada fila
    del archivo. Un tenant que importa siempre tiene `BusinessProfile`.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.business import BusinessProfile  # noqa: PLC0415

    result = await session.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    return parse_vertical(result.scalar_one_or_none())


# ── FASE 3: vínculo de entidades (ventas/gastos → producto del catálogo) ──────


# Mejora B: tokens genéricos de unidad/medida que NO identifican un producto.
_PRODUCT_TOKEN_STOPWORDS: frozenset[str] = frozenset(
    {"kg", "und", "x", "de", "la", "el", "ml", "gr"}
)


def _product_name_tokens(name: str) -> list[str]:
    """Tokens significativos del nombre normalizado (≥3 chars, sin stopwords).

    Reusa ``_normalize_name`` (guiones/underscores → espacio) para que
    "Coca-Cola 500ml" y "coca cola 500" produzcan tokens comparables.
    """
    norm = _normalize_name(name)
    return [
        t for t in norm.split(" ") if len(t) >= 3 and t not in _PRODUCT_TOKEN_STOPWORDS
    ]


async def _load_product_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID | None], dict[str, set[uuid.UUID]]]:
    """Carga el catálogo del tenant UNA vez para vincular transacciones en memoria.

    Evita N queries (una por fila). Devuelve `(by_sku, by_name, by_token)`:
    - `by_sku[sku_lower] = product_id`
    - `by_name[norm_name] = product_id` o `None` si el nombre normalizado es
      ambiguo (varios productos lo comparten → no se vincula).
    - `by_token[token] = {product_id, ...}` para match conservador por tokens
      (Mejora B): token ≥3 chars del nombre, sin stopwords genéricos de unidad.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415

    result = await session.execute(
        select(Product.id, Product.name, Product.sku).where(Product.tenant_id == tenant_id)
    )
    by_sku: dict[str, uuid.UUID] = {}
    by_name: dict[str, uuid.UUID | None] = {}
    by_token: dict[str, set[uuid.UUID]] = {}
    for pid, pname, psku in result.all():
        # F2-T4: clave de SKU canónica (``normalize_sku``), misma que el motor de
        # identidad — antes era ``str(psku).strip().lower()`` (sin NFKD/colapso).
        sku_key = normalize_sku(psku)
        if sku_key:
            by_sku[sku_key] = pid
        norm = _normalize_name(pname or "")
        if norm:
            by_name[norm] = pid if norm not in by_name else None  # None = ambiguo
        for tok in _product_name_tokens(pname or ""):
            by_token.setdefault(tok, set()).add(pid)
    return by_sku, by_name, by_token


# ── F2-T2: resolución de identidad de producto por claves independientes ──────
# Reemplaza el lookup name-only de F1 (``_find_product_by_name_tolerant`` +
# ``_load_product_name_lookup_indexes``, eliminados — ver historia en
# ``test_ingestion_product_name_collision.py``). Orden barcode → sku →
# nombre+marca, NO jerárquico excluyente: cada clave restringe (narrowing)
# independientemente el conjunto de candidatos válidos. Detecta tanto
# AMBIGÜEDAD (un tier con ≥2 candidatos que ningún otro tier logra achicar a
# 1) como CONFLICTO (dos tiers que apuntan a productos DISTINTOS, sin
# intersección). El import de archivos hoy no parsea barcode (fase
# posterior) — el motor lo contempla igual, para reuso (T2b / POST /products).

_IDENTITY_TIER_MATCHED_BY: dict[str, frozenset[str]] = {
    "barcode": frozenset({"barcode"}),
    "sku": frozenset({"sku"}),
    "name+brand": frozenset({"name", "brand"}),
    "name": frozenset({"name"}),
}


class ProductIdentityIndexes(NamedTuple):
    """Índices en memoria de productos ACTIVOS del tenant, UNA carga por corrida."""

    by_sku: dict[str, list[uuid.UUID]]
    by_barcode: dict[str, list[uuid.UUID]]
    by_name_brand: dict[tuple[str, str | None], list[uuid.UUID]]
    by_name: dict[str, list[uuid.UUID]]
    by_id: dict[uuid.UUID, dict[str, Any]]


class ProductResolution(NamedTuple):
    """Resultado de ``_resolve_product_identity``.

    ``status``: ``"resolved"`` (1 único producto) | ``"create"`` (ningún tier
    matcheó) | ``"ambiguous"`` (un tier con ≥2 candidatos, sin otro tier que
    lo achique) | ``"conflict"`` (tiers distintos apuntan a productos
    DISTINTOS). ``candidates`` solo se llena para ambiguous/conflict — forma
    para ``match_candidates``: ``{id, matched_by, name, sku, barcode}``.
    """

    status: str
    product_id: uuid.UUID | None
    candidates: list[dict[str, Any]]


async def _load_product_identity_indexes(
    session: AsyncSession, tenant_id: uuid.UUID
) -> ProductIdentityIndexes:
    """Precarga (F2-T2) los índices de identidad de producto, UNA vez por
    corrida — nunca un ``select(Product)`` de entidades completas por fila.

    Usa las columnas ``*_normalized`` persistidas por el listener de T1
    (``app/persistence/models/product.py``), no recalcula. Solo productos
    ACTIVOS del tenant, ordenados por ``(created_at, id)`` ascendente
    (desempate estable).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415

    result = await session.execute(
        select(
            Product.id,
            Product.name,
            Product.sku,
            Product.barcode,
            Product.custom_fields,
            Product.sku_normalized,
            Product.name_normalized,
            Product.brand_normalized,
            Product.barcode_normalized,
            Product.created_at,
        )
        .where(Product.tenant_id == tenant_id, Product.is_active.is_(True))
        .order_by(Product.created_at.asc(), Product.id.asc())
    )
    by_sku: dict[str, list[uuid.UUID]] = {}
    by_barcode: dict[str, list[uuid.UUID]] = {}
    by_name_brand: dict[tuple[str, str | None], list[uuid.UUID]] = {}
    by_name: dict[str, list[uuid.UUID]] = {}
    by_id: dict[uuid.UUID, dict[str, Any]] = {}
    for (
        pid,
        pname,
        psku,
        pbarcode,
        pcustom,
        sku_n,
        name_n,
        brand_n,
        barcode_n,
        _created_at,
    ) in result.all():
        by_id[pid] = {"name": pname, "sku": psku, "barcode": pbarcode}
        # Defensa contra el bloqueante de review: los productos LEGACY (previos
        # al listener de T1) tienen las columnas ``*_normalized`` en NULL y sin
        # esto quedarían fuera de todo índice → una importación los daría por
        # inexistentes y crearía un duplicado. Si la columna persistida vino
        # NULL, se computa la clave on-the-fly con los MISMOS normalizadores
        # canónicos del listener (misma fuente de cálculo). La migración de
        # backfill (20260731_0002) hace esto permanente en la DB; este fallback
        # cubre la ventana previa a su corrida y cualquier drift.
        if not name_n:
            name_n = normalize_product_name(pname) or None
        if not sku_n:
            sku_n = normalize_sku(psku)
        if not barcode_n:
            barcode_n = normalize_barcode(pbarcode)
        if not brand_n:
            marca = pcustom.get("marca") if isinstance(pcustom, dict) else None
            brand_n = normalize_brand(marca)
        if sku_n:
            by_sku.setdefault(sku_n, []).append(pid)
        if barcode_n:
            by_barcode.setdefault(barcode_n, []).append(pid)
        if name_n:
            by_name.setdefault(name_n, []).append(pid)
            by_name_brand.setdefault((name_n, brand_n), []).append(pid)
    return ProductIdentityIndexes(by_sku, by_barcode, by_name_brand, by_name, by_id)


def _resolve_product_identity(
    name: str | None,
    sku: str | None,
    brand: str | None,
    *,
    indexes: ProductIdentityIndexes,
    barcode: str | None = None,
) -> ProductResolution:
    """Motor puro (sin session) de resolución de identidad de producto.

    Calcula las claves de la fila con los normalizadores de ``text_norm`` y
    evalúa, en orden de prioridad barcode → sku → nombre+marca, cada tier que
    matchea contra los índices pre-cargados. El conjunto de candidatos se va
    "achicando" por intersección: si un tier de mayor prioridad ya resolvió a
    un único id y un tier posterior es ambiguo pero ese id está entre sus
    candidatos, la intersección lo deja en 1 (resuelto — así el SKU desambigua
    un nombre repetido). Si la intersección es vacía, dos tiers apuntan a
    productos DISTINTOS → conflicto. Si al final quedan ≥2 candidatos sin que
    ningún tier los pueda achicar → ambiguo. Nunca adivina.
    """
    sku_n = normalize_sku(sku)
    name_n = normalize_product_name(name)
    brand_n = normalize_brand(brand)
    bc_n = normalize_barcode(barcode)

    tiers: list[tuple[str, list[uuid.UUID]]] = []
    if bc_n:
        bc_ids = indexes.by_barcode.get(bc_n)
        if bc_ids:
            tiers.append(("barcode", bc_ids))
    if sku_n:
        sku_ids = indexes.by_sku.get(sku_n)
        if sku_ids:
            tiers.append(("sku", sku_ids))
    if brand_n:
        nb_ids = indexes.by_name_brand.get((name_n, brand_n))
        if nb_ids:
            tiers.append(("name+brand", nb_ids))
    elif name_n:
        n_ids = indexes.by_name.get(name_n)
        if n_ids:
            tiers.append(("name", n_ids))

    if not tiers:
        return ProductResolution("create", None, [])

    matched_by: dict[uuid.UUID, set[str]] = {}
    order: list[uuid.UUID] = []
    for tier_name, ids in tiers:
        for pid in ids:
            if pid not in matched_by:
                matched_by[pid] = set()
                order.append(pid)
            matched_by[pid].update(_IDENTITY_TIER_MATCHED_BY[tier_name])

    candidate_set: set[uuid.UUID] = set(tiers[0][1])
    conflict = False
    for _tier_name, ids in tiers[1:]:
        id_set = set(ids)
        intersected = candidate_set & id_set
        if intersected:
            candidate_set = intersected
        else:
            conflict = True
            candidate_set = candidate_set | id_set

    def _candidates(ids: list[uuid.UUID]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pid in ids:
            info = indexes.by_id.get(pid, {})
            out.append(
                {
                    "id": str(pid),
                    "matched_by": sorted(matched_by.get(pid, set())),
                    "name": info.get("name"),
                    "sku": info.get("sku"),
                    "barcode": info.get("barcode"),
                }
            )
        return out

    # FIX B (review de T2): ``match_candidates`` se armaba desde ``order``
    # (la unión de TODOS los ids vistos en CUALQUIER tier) en vez del
    # conjunto final post-intersección/narrowing (``candidate_set``) — un id
    # que un tier posterior descartó por narrowing seguía apareciendo en
    # ``match_candidates``, confundiendo la revisión manual en /otros. Se
    # filtra ``order`` (preserva un orden estable) contra el ``candidate_set``
    # final: exactamente los ids que causaron el ambiguous/conflict, ni más
    # ni menos. ``matched_by`` de cada candidato sigue reflejando TODOS los
    # tiers que lo matchearon (no solo los del conjunto final).
    if conflict:
        final_ids = [pid for pid in order if pid in candidate_set]
        return ProductResolution("conflict", None, _candidates(final_ids))
    if len(candidate_set) >= 2:
        final_ids = [pid for pid in order if pid in candidate_set]
        return ProductResolution("ambiguous", None, _candidates(final_ids))
    return ProductResolution("resolved", next(iter(candidate_set)), [])


def _product_identity_lookup_keys(
    sku_n: str | None, name_n: str, brand_n: str | None, barcode_n: str | None = None
) -> list[str]:
    """Claves de BÚSQUEDA en la caché intra-corrida (F2-T2, fix de review),
    en el MISMO orden de prioridad que el motor (``_resolve_product_identity``):
    barcode → sku → nombre+marca → nombre. Se prueban en orden, primer hit gana.

    Replica la restricción del motor: si la fila TIENE marca, el tier
    name-only NUNCA se consulta (con marca presente, el motor resuelve por
    nombre+marca, jamás por nombre solo) — así dos filas del mismo archivo
    con el mismo nombre pero DISTINTA marca no se fusionan vía la clave
    name-only que dejó registrada un producto de otra marca.
    """
    keys: list[str] = []
    if barcode_n:
        keys.append(f"barcode:{barcode_n}")
    if sku_n:
        keys.append(f"sku:{sku_n}")
    if brand_n:
        keys.append(f"nb:{name_n}|{brand_n}")
    else:
        keys.append(f"name:{name_n}")
    return keys


def _product_identity_register_keys(
    sku_n: str | None, name_n: str, brand_n: str | None, barcode_n: str | None = None
) -> list[str]:
    """Claves bajo las que se REGISTRA en la caché un producto ya
    resuelto/creado (F2-T2, fix de review): todas las aplicables, no una
    sola. Bug corregido: la caché de una sola clave (``sku:`` si había sku,
    si no ``nb:``) duplicaba productos cuando 2 filas del MISMO archivo son
    el MISMO producto lógico pero difieren en si traen SKU — fila1 "Fideos"
    con sku registraba solo ``sku:x1``; fila2 "Fideos" sin sku buscaba
    ``nb:fideos|`` (miss) y creaba un segundo producto. Registrando bajo
    TODAS las claves (barcode si hay, sku si hay, nombre+marca, y nombre
    solo) una fila posterior sin sku encuentra el producto vía ``name:fideos``.
    """
    keys: list[str] = []
    if barcode_n:
        keys.append(f"barcode:{barcode_n}")
    if sku_n:
        keys.append(f"sku:{sku_n}")
    keys.append(f"nb:{name_n}|{brand_n or ''}")
    keys.append(f"name:{name_n}")
    return keys


def _lookup_product_identity_cache(
    cache: dict[str, Product],
    sku_n: str | None,
    name_n: str,
    brand_n: str | None,
    barcode_n: str | None = None,
) -> Product | None:
    """Busca en la caché intra-corrida por las claves de la fila, en orden
    de prioridad. Primer hit gana."""
    for key in _product_identity_lookup_keys(sku_n, name_n, brand_n, barcode_n):
        hit = cache.get(key)
        if hit is not None:
            return hit
    return None


def _register_product_identity_cache(
    cache: dict[str, Product],
    product: Product,
    sku_n: str | None,
    name_n: str,
    brand_n: str | None,
    barcode_n: str | None = None,
) -> None:
    """Registra el producto resuelto/creado bajo todas sus claves de
    identidad aplicables."""
    for key in _product_identity_register_keys(sku_n, name_n, brand_n, barcode_n):
        cache[key] = product


# ── F2-T4/T5: resolución de identidad UNIFICADA para VINCULAR transacciones ────
# El mismo motor (``_resolve_product_identity``) que usa el bucket de catálogo se
# comparte ahora para ventas, gastos y compras (review F2 #1/#5). Antes esos
# caminos usaban ``_resolve_product`` (solo sku+nombre, sin barcode/marca, sin
# distinguir ambiguo de inexistente) → una compra ambigua se interpretaba como
# "no existe" y creaba un TERCER producto duplicado.


def _resolve_link(
    name: str | None,
    sku: str | None,
    brand: str | None,
    barcode: str | None,
    *,
    indexes: ProductIdentityIndexes,
    cache: dict[str, Product],
) -> ProductResolution:
    """Resuelve la identidad de un producto para VINCULAR una transacción. Consulta
    primero la caché intra-corrida (productos creados/resueltos en esta corrida) y,
    en miss, el motor de identidad. Devuelve el ``ProductResolution`` tal cual — el
    caller decide qué hacer con ``resolved``/``create``/``ambiguous``/``conflict``.
    """
    sku_n = normalize_sku(sku)
    name_n = normalize_product_name(name)
    brand_n = normalize_brand(brand)
    bc_n = normalize_barcode(barcode)
    hit = _lookup_product_identity_cache(cache, sku_n, name_n, brand_n, bc_n)
    if hit is not None:
        return ProductResolution("resolved", hit.id, [])
    return _resolve_product_identity(name, sku, brand, indexes=indexes, barcode=barcode)


def _candidates_from_conflict(
    conflict: ProductIdentityConflictError,
) -> list[dict[str, Any]]:
    """``match_candidates`` desde una ambigüedad detectada por la DB.

    Misma forma que el ``_candidates`` de ``_resolve_product_identity``
    (``{id, matched_by, name, sku, barcode}``) para que ``/otros`` renderice igual
    una ambigüedad del motor y una detectada por el índice único. Acá ``matched_by``
    sale de qué clave comparte cada candidato con la fila, no del índice violado.
    """
    # ``existing`` ocupa la clave que reportó la DB; ``other``, por definición, la otra.
    otra: MatchedBy = "sku" if conflict.matched_by == "barcode" else "barcode"
    porque: list[tuple[Product, str]] = [(conflict.existing, conflict.matched_by)]
    if conflict.other is not None:
        porque.append((conflict.other, otra))
    return [
        {
            "id": str(product.id),
            "matched_by": [matched],
            "name": product.name,
            "sku": product.sku,
            "barcode": product.barcode,
        }
        for product, matched in porque
    ]


async def _resolve_purchase_identity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str | None,
    sku: str | None,
    brand: str | None,
    barcode: str | None,
    unit_cost: Decimal | None,
    indexes: ProductIdentityIndexes,
    cache: dict[str, Product],
    product_cache: dict[uuid.UUID, Any] | None = None,
) -> tuple[str, uuid.UUID | None, list[dict[str, Any]]]:
    """Resuelve el producto de una COMPRA de mercadería con el motor unificado.

    Devuelve ``(action, product_id, candidates)``:
      - ``("linked", id, [])``  → producto existente (``resolved``): vincular.
      - ``("created", id, [])`` → ningún match (``create``): crea producto incompleto
        (con barcode) y lo registra en la caché intra-corrida.
      - ``("otros", None, cands)`` → ``ambiguous``/``conflict``: NO crea (evita el
        3er duplicado del review F2 #1) — el caller rutea la fila a "Otros".

    F5-A: los índices se pre-cargan al abrir la corrida, así que el motor puede
    decidir ``create`` sobre una clave que otra transacción ocupó en el medio. Si el
    índice único la rechaza, ``build_incomplete_product`` reusa al ocupante y esto
    devuelve ``"linked"``, NO ``"created"`` — si no, ``counts``/``products_created``
    reportarían un producto que nunca se creó.
    """
    res = _resolve_link(name, sku, brand, barcode, indexes=indexes, cache=cache)
    if res.status == "resolved":
        return "linked", res.product_id, []
    if res.status in ("ambiguous", "conflict"):
        return "otros", None, res.candidates
    try:
        new_id, created = await build_incomplete_product(
            session, tenant_id, name, sku, unit_cost, product_cache, barcode=barcode
        )
    except ProductIdentityConflictError as conflict:
        # Ambigüedad detectada por la DB (barcode y sku en productos distintos): mismo
        # destino que la ambigüedad detectada por el motor — "Otros", nunca adivinar.
        logger.warning(
            "ingestion.product_identity_ambiguous_on_insert",
            tenant_id=str(tenant_id),
            matched_by=conflict.matched_by,
            candidate_ids=[str(p.id) for p in conflict.candidates],
        )
        return "otros", None, _candidates_from_conflict(conflict)
    if new_id is None:
        return "otros", None, []  # sin nombre utilizable: nada que crear ni linkear
    # ``build_incomplete_product`` cachea por id en ``product_cache`` tanto el ORM
    # recién creado como el ocupante reusado; lo registramos en la caché de identidad
    # para que filas POSTERIORES del mismo archivo lo reusen (no dupliquen).
    # También en el camino de REUSO: si no, cada fila siguiente con esa clave vuelve
    # a construir el Product, abre un savepoint, se come el IntegrityError y
    # re-consulta al ocupante — N savepoints y 2N roundtrips evitables en el camino
    # caliente del import.
    resuelto = product_cache.get(new_id) if product_cache is not None else None
    if resuelto is not None:
        _register_product_identity_cache(
            cache,
            resuelto,
            normalize_sku(sku),
            normalize_product_name(name),
            normalize_brand(brand),
            normalize_barcode(barcode),
        )
    if not created:
        return "linked", new_id, []  # el índice único resolvió una carrera
    return "created", new_id, []


def _register_product_transaction_indexes(
    product_id: uuid.UUID,
    name: str | None,
    sku: str | None,
    by_sku: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID | None],
    by_token: dict[str, set[uuid.UUID]],
) -> None:
    """Registra un producto (creado o resuelto por compra) en los índices
    transaccionales del `_resolve_product` para que ventas/gastos POSTERIORES del
    MISMO archivo puedan encontrarlo (review F2 #2). El motor de compras solo
    puebla `products_by_identity_key`; sin esto, `_resolve_product` (que usa estos
    índices) no ve el producto y una venta same-file queda sin vincular.

    Ambiguity-safe (a diferencia del viejo `_ensure_product_for_purchase`, que
    sobrescribía a ciegas): si el nombre normalizado ya apuntaba a OTRO producto,
    queda marcado ambiguo (`None`) en vez de resolverse arbitrariamente al nuevo.
    """
    sku_key = normalize_sku(sku)
    if sku_key:
        by_sku[sku_key] = product_id
    clean_name = _clean_str(name, 299)
    if clean_name:
        norm = _normalize_name(clean_name)
        if norm:
            if norm not in by_name:
                by_name[norm] = product_id
            elif by_name[norm] != product_id:
                by_name[norm] = None  # nombre ya ambiguo → no resolver a ciegas
        for tok in _product_name_tokens(clean_name):
            by_token.setdefault(tok, set()).add(product_id)


def _resolve_product(
    by_sku: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID | None],
    name: str | None,
    sku: str | None,
    by_token: dict[str, set[uuid.UUID]] | None = None,
    *,
    by_barcode: dict[str, list[uuid.UUID]] | None = None,
    barcode: str | None = None,
) -> uuid.UUID | None:
    """Resuelve product_id desde el índice en memoria (link de ventas/gastos/
    compras contra el catálogo).

    Tiers, en orden de prioridad: (0) F2-T5 barcode exacto (EAN/UPC — el
    identificador más fuerte; solo si matchea a un único producto), (1) SKU
    exacto, (2) nombre normalizado exacto, (3) Mejora B: intersección
    conservadora de tokens. El tier de tokens solo acepta cuando la intersección
    de los productos que comparten los tokens del nombre de entrada tiene
    EXACTAMENTE un id (0 o >1 → abstención, sin inventar). El barcode/sku ganan
    sobre el nombre — un identificador fuerte desambigua un nombre repetido.
    """
    if barcode and by_barcode is not None:
        bc_ids = by_barcode.get(normalize_barcode(barcode) or "")
        if bc_ids and len(bc_ids) == 1:  # único → clave fuerte; ambiguo → no inventar
            return bc_ids[0]
    if sku:
        hit = by_sku.get(normalize_sku(sku) or "")  # F2-T4: clave canónica
        if hit:
            return hit
    if name:
        norm = _normalize_name(str(name))
        if norm and norm in by_name:
            return by_name[norm]  # None si es ambiguo (no degradar a tokens)
        # Tier 3 (Mejora B): match conservador por tokens. Solo si el nombre
        # normalizado NO existía en by_name (ni exacto ni ambiguo).
        if by_token:
            tokens = sorted(
                _product_name_tokens(str(name)), key=len, reverse=True
            )
            candidates: set[uuid.UUID] | None = None
            for tok in tokens:
                ids = by_token.get(tok)
                if not ids:
                    continue
                candidates = set(ids) if candidates is None else (candidates & ids)
                if not candidates:
                    break
            if candidates is not None and len(candidates) == 1:
                return next(iter(candidates))
    return None


async def _load_balance_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[uuid.UUID, Any]:
    """Carga TODOS los InventoryBalance del tenant en una query (batch).

    Evita un SELECT por fila en imports con movimientos de stock: el balance
    se busca/actualiza en memoria y los nuevos se registran en el índice.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.inventory import InventoryBalance  # noqa: PLC0415

    result = await session.execute(
        select(InventoryBalance).where(InventoryBalance.tenant_id == tenant_id)
    )
    return {b.product_id: b for b in result.scalars().all()}


async def _record_stock_movement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    qty: int,
    unit_cost: Decimal | None,
    movement_type: str,
    final_qty: int,
    balance_index: dict[uuid.UUID, Any] | None = None,
    supplier_id: uuid.UUID | None = None,
    source_type: str | None = None,
    source_upload_id: uuid.UUID | None = None,
    source_row_ref: str | None = None,
    source_row_hash: str | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """FASE 3: registra un InventoryMovement (audit insert-only) y sincroniza el
    `InventoryBalance` por el import, dentro de la misma transacción.

    A2: estampa el ORIGEN del movimiento (``source_type``/``source_upload_id``/
    ``source_row_ref``/``source_row_hash`` — spec en ``inventory_movement_origin``)
    para dedup, reversa del reread y reconciliación. Los valores los arma el caller
    (el import conoce archivo, fila y campos de la fila de origen).

    ``occurred_at``: fecha de NEGOCIO del movimiento (cuándo ocurrió la compra/ajuste
    en el mundo real), NO la fecha de carga del archivo. El caller la arma desde la
    fila de origen (``ExpenseEntry.transaction_date`` o la fecha del catálogo); si no
    hay fecha disponible queda ``NULL`` a propósito (los lectores caen a
    ``COALESCE(occurred_at, created_at)`` — nunca se inventa una fecha).

    `Product.stock_units` sigue siendo la representación canónica que lee la UI; el
    import la setea. Esta función:
      1. Inserta el movimiento de inventario (historial, antes vacío al importar).
      2. Sincroniza `InventoryBalance.current_qty`: `+= qty` (delta) si el balance
         existe; lo crea en `final_qty` (el stock total tras el import) si no, para
         mantenerlo consistente con `Product.stock_units`.

    NO hace flush ni emite EventBus por fila (a diferencia de
    `stock_service.increment_stock`): en un import masivo eso sería caro/ruidoso;
    todo entra en el commit batch del import. `qty>0` = ingreso, `qty<0` = ajuste.
    `qty==0` → no-op (ni movimiento ni balance).
    """
    if qty == 0:
        return
    if movement_type == "adjustment" and source_type is None:
        raise ValueError(
            "movement_type='adjustment' requiere source_type "
            "(ver app/application/services/inventory_movement_origin.py) — "
            "un ajuste sin origen trazable no puede reconciliarse ni auditarse."
        )
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.inventory import (  # noqa: PLC0415
        InventoryBalance,
        InventoryMovement,
    )

    session.add(
        InventoryMovement(
            tenant_id=tenant_id,
            product_id=product_id,
            supplier_id=supplier_id,
            movement_type=movement_type,
            qty=qty,
            unit_cost=unit_cost,
            source_event_id="import",
            reason="Importado desde archivo",
            # A2: origen del movimiento (traza + dedup + reversa del reread).
            source_type=source_type,
            source_upload_id=source_upload_id,
            source_row_ref=source_row_ref,
            source_row_hash=source_row_hash,
            occurred_at=ensure_utc(occurred_at),
        )
    )

    if balance_index is not None:
        balance = balance_index.get(product_id)
    else:
        balance = (
            await session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.product_id == product_id,
                    InventoryBalance.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if balance is None:
        # F5-A: acotado a productos NUEVOS (los preexistentes ya tienen balance y caen
        # en el `else`). Si otra transacción creó el balance en el medio, el índice
        # único lo rechaza y se reusa el suyo, sumándole `qty` —el mismo efecto que la
        # rama `else`, no `final_qty` absoluto, que pisaría lo que el otro escribió—.
        try:
            async with guarded_savepoint(session, stock_service.BALANCE_CONFLICT):
                new_balance = InventoryBalance(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    current_qty=final_qty,
                    reserved_qty=0,
                )
                session.add(new_balance)
        except SavepointConflictError as conflict:
            existing_balance = (
                await session.execute(
                    select(InventoryBalance).where(
                        InventoryBalance.product_id == product_id,
                        InventoryBalance.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_balance is None:  # pragma: no cover — el índice lo garantiza
                raise conflict.original from conflict
            existing_balance.current_qty += qty
            if balance_index is not None:
                balance_index[product_id] = existing_balance
            return
        if balance_index is not None:
            balance_index[product_id] = new_balance
    else:
        balance.current_qty += qty


def _parse_qty(qty_raw: Any) -> int:
    """Cantidad entera de una celda de compra. 0 si vacía/no parseable/negativa."""
    if qty_raw in (None, "", "None", "nan"):
        return 0
    try:
        qty = int(float(str(qty_raw)))
    except (ValueError, TypeError):
        return 0
    return qty if qty > 0 else 0


async def build_incomplete_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str | None,
    sku: str | None,
    unit_cost: Decimal | None,
    product_cache: dict[uuid.UUID, Any] | None = None,
    *,
    barcode: str | None = None,
) -> tuple[uuid.UUID | None, bool]:
    """Construye y agrega a la sesión un ``Product`` vendible INCOMPLETO desde una
    compra de mercadería, devolviendo ``(id, creado)``.

    Un producto nacido de una compra no trae precio de venta ni categoría de
    catálogo: nace ``requires_completion=True``, ``sale_price_ars=0``,
    ``unit_cost_ars`` del costo si vino, ``stock_units=0`` (el stock lo incrementa
    quien corresponda después). Único lugar donde se materializa este patrón de
    "producto desde compra", reusado por el import (``_ensure_product_for_purchase``)
    y por la reclasificación de gastos a reventa (``RECLASSIFY_EXPENSE``).

    Si se pasa ``product_cache``, el objeto resultante se cachea por id. Crítico
    con ``autoflush=False`` (prod): ``session.get`` no ve un producto pendiente sin
    flush, así que ``_apply_purchase_to_stock`` no podría incrementarle el stock —
    el cache se lo entrega sin tocar la DB.

    F5-A: el INSERT va por ``add_product_or_reuse``. Si el SKU/barcode ya está tomado
    por un producto activo del tenant —una carrera, o un índice pre-cargado que no lo
    vio— devuelve ``(id_existente, False)`` en vez de romper: violar la unicidad de
    una clave FUERTE no es ambigüedad, es el match exacto que ``_resolve_link``
    hubiera devuelto como ``resolved``. La ambigüedad real (barcode y sku en
    productos DISTINTOS) sí levanta ``ProductIdentityConflictError`` — no hay "el
    existente" que reusar y el caller debe rutear la fila a "Otros".

    Es ``async`` desde F5-A: el savepoint necesita un flush por producto NUEVO
    distinto (no por fila).

    INVARIANTE PARA CALLERS SIN ``except ProductIdentityConflictError``
    (``data_repair_service``, ``pending_action_service``): solo es seguro no
    capturarla mientras se llame con **a lo sumo una** clave fuerte. La ambigüedad
    requiere que barcode Y sku tengan dueños distintos, así que con ``barcode=None``
    nunca se levanta. El día que alguien le agregue ``barcode=`` a esas llamadas,
    aparece un 500 sin diagnóstico — hay que agregar el ``except`` primero.
    """
    from app.persistence.models.product import Product  # noqa: PLC0415

    clean_name = _clean_str(name, 299)
    if not clean_name:
        return None, False
    clean_sku = _clean_str(sku, 99)
    clean_barcode = _clean_str(barcode, 64)  # F2-T5

    new_id = uuid.uuid4()
    product = Product(
        id=new_id,
        tenant_id=tenant_id,
        name=clean_name,
        sku=clean_sku,
        barcode=clean_barcode,  # F2-T5
        sale_price_ars=Decimal("0"),  # una compra no trae precio de venta
        unit_cost_ars=unit_cost,
        stock_units=0,  # el incremento lo hace quien corresponda después
        # category de PRODUCTO (vertical) ≠ código de gasto → None (incompleto).
        category=None,
        low_stock_threshold_units=None,
        provenance="REAL",
        requires_completion=True,  # falta precio de venta → completar
    )
    # NO ``session.add()`` acá: ``add_product_or_reuse`` exige el objeto TRANSIENT
    # para poder emitir el INSERT DENTRO del savepoint (ver services/_savepoint.py).
    resolved, created = await add_product_or_reuse(session, product)
    if product_cache is not None:
        product_cache[resolved.id] = resolved
    return resolved.id, created


async def _ensure_product_for_purchase(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str | None,
    sku: str | None,
    unit_cost: Decimal | None,
    by_sku: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID | None],
    by_token: dict[str, set[uuid.UUID]] | None = None,
    product_cache: dict[uuid.UUID, Any] | None = None,
) -> uuid.UUID | None:
    """Crea el ``Product`` de una compra de mercadería cuyo SKU/nombre NO está en
    el catálogo, y devuelve su id (o ``None`` si no hay nombre utilizable).

    Específico de "compra desde gasto": a diferencia del bloque ``wants_productos``
    (que SETEA stock absoluto y precio de venta del archivo), acá el producto nace
    INCOMPLETO —``requires_completion=True``, ``sale_price_ars=0`` (una compra no
    trae precio de venta), ``unit_cost_ars`` del costo si vino— y el stock lo
    INCREMENTA luego ``_apply_purchase_to_stock``. Delega la construcción del ORM
    en ``build_incomplete_product`` (helper compartido) y solo agrega el cacheo
    por SKU/nombre para no duplicar producto entre filas del mismo archivo.

    Cachea el id nuevo en ``by_sku``/``by_name`` para que filas posteriores del
    mismo SKU/nombre en el mismo archivo reusen el producto (sin duplicar).
    """
    new_id, _created = await build_incomplete_product(
        session, tenant_id, name, sku, unit_cost, product_cache=product_cache
    )
    if new_id is None:
        return None
    clean_name = _clean_str(name, 299)
    clean_sku = _clean_str(sku, 99)
    # Cachear para que filas repetidas del mismo SKU/nombre no dupliquen producto.
    # F2-T4: clave canónica (``normalize_sku``), consistente con _load_product_index.
    sku_key = normalize_sku(clean_sku)
    if sku_key:
        by_sku[sku_key] = new_id
    if clean_name:
        norm = _normalize_name(clean_name)
        if norm:
            by_name[norm] = new_id
        if by_token is not None:
            for tok in _product_name_tokens(clean_name):
                by_token.setdefault(tok, set()).add(new_id)
    return new_id


async def _apply_purchase_to_stock(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    expense: Any,
    qty_raw: Any,
    unit_cost: Decimal | None,
    balance_index: dict[uuid.UUID, Any] | None = None,
    product_cache: dict[uuid.UUID, Any] | None = None,
    source_type: str = SOURCE_PURCHASE_IMPORT,
    source_row_ref: str | None = None,
) -> None:
    """FASE D: una compra de mercadería importada suma stock.

    Gate doble contra falsos positivos / doble conteo:
      - el gasto debe tener producto del catálogo resuelto (`product_id`); y
      - la fila debe traer una columna de cantidad con valor > 0.
    A diferencia del import de productos (que SETEA stock absoluto), acá se
    INCREMENTA: `Product.stock_units += qty` + movimiento de inventario + sync
    de balance, igual que `stock_service.increment_stock` pero sin flush por
    fila (batch del import).
    """
    if expense.product_id is None:
        return
    try:
        qty = int(float(str(qty_raw))) if qty_raw not in (None, "", "None", "nan") else 0
    except (ValueError, TypeError):
        return
    if qty <= 0:
        return
    from app.persistence.models.product import Product  # noqa: PLC0415

    # Cache primero: con autoflush=False un producto recién creado (pendiente) NO
    # aparece en session.get; el cache lo entrega. Para los existentes evita un
    # session.get por fila (se cachea tras el primer acceso).
    product = product_cache.get(expense.product_id) if product_cache is not None else None
    if product is None:
        product = await session.get(Product, expense.product_id)
        if product is None or product.tenant_id != tenant_id:
            return
        if product_cache is not None:
            product_cache[expense.product_id] = product
    elif product.tenant_id != tenant_id:
        return
    product.stock_units += qty
    if unit_cost is not None:
        product.unit_cost_ars = unit_cost
    # A2: identidad lógica estable de la fila de origen (idempotencia + reconciliación).
    _row_hash = compute_source_row_hash(
        product_key=product.name,
        qty=qty,
        unit_cost=unit_cost,
        movement_date=getattr(expense, "transaction_date", None),
        supplier_key=(
            getattr(expense, "supplier_name", None)
            or getattr(expense, "supplier_id", None)
        ),
        upload_id=getattr(expense, "source_upload_id", None),
    )
    await _record_stock_movement(
        session,
        tenant_id,
        product.id,
        qty,
        unit_cost,
        "purchase",
        product.stock_units,
        balance_index=balance_index,
        # FASE 3: el movimiento de compra hereda el proveedor del gasto (real o
        # sentinela "No identificado"); NULL si la compra no traía proveedor.
        supplier_id=getattr(expense, "supplier_id", None),
        # A2: origen de la compra (purchase_import por defecto; receipt para remitos).
        source_type=source_type,
        source_upload_id=getattr(expense, "source_upload_id", None),
        source_row_ref=source_row_ref,
        source_row_hash=_row_hash,
        occurred_at=getattr(expense, "transaction_date", None),
    )


def _add_catalog_initial_stock_cogs(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    product_name: str,
    qty: int,
    unit_cost: Decimal | None,
    tx_date: datetime,
    source_upload_id: uuid.UUID | None,
    source_row_ref: str | None,
) -> None:
    """Crea el gasto de mercadería (COGS) de un stock de catálogo tratado como COMPRA.

    Solo se invoca cuando el usuario marcó el stock como compra (``is_purchase`` en
    ``_apply_catalog_stock``), NO para saldo de apertura. Regla contable: una compra de
    mercadería se refleja como gasto ``INVENTORY``/``COGS`` (monto = ``unit_cost × qty``)
    + salida de caja, ligado al producto, con la misma fila de origen.

    Sin costo conocido no se puede valuar la compra → se omite el gasto (el producto
    ya nace ``requires_completion=True`` y el usuario lo completa después).
    ``qty <= 0`` → no-op (no entró stock nuevo; un ``_delta`` negativo es un ajuste,
    no una compra).
    """
    if qty <= 0 or unit_cost is None or unit_cost <= 0:
        return
    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

    expense = ExpenseEntry(
        tenant_id=tenant_id,
        amount=(unit_cost * qty).quantize(Decimal("0.01")),
        category="INVENTORY",
        # Producto ligado + categoría INVENTORY ⇒ COGS (mismo helper que el resto).
        expense_type=infer_expense_type("INVENTORY", product_id=product_id),
        transaction_date=tx_date,
        description=f"Stock inicial: {product_name}"[:500],
        is_recurring=False,
        payment_method="transfer",
        provenance="REAL",
        product_id=product_id,
        source_upload_id=source_upload_id,
    )
    if source_row_ref is not None:
        expense.source_row_ref = source_row_ref
    session.add(expense)


async def _apply_catalog_stock(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    product_name: str,
    delta: int,
    final_qty: int,
    unit_cost: Decimal | None,
    store_name: str | None,
    tx_date: datetime,
    uploaded_file_id: uuid.UUID | None,
    source_row_ref: str | None,
    balance_index: dict[uuid.UUID, Any] | None,
    is_purchase: bool = False,
) -> None:
    """Aplica el stock de una fila de CATÁLOGO al inventario, según su tratamiento.

    Un archivo de catálogo/lista de stock es AMBIGUO: el stock puede ser **saldo de
    apertura** (mercadería que el negocio YA tenía al empezar con Véktor — un activo,
    no un gasto) o una **compra** (la adquirió ahora → gasto de mercadería + baja de
    caja). El usuario lo aclara en el confirm (``stock_treatment``); ``is_purchase`` es
    ese flag ya resuelto.

    - ``is_purchase=False`` (saldo de apertura, DEFAULT): el stock entra al inventario
      como **ajuste** (``movement_type='adjustment'``), SIN ``ExpenseEntry`` ni salida
      de caja. No cuenta como "comprado".
    - ``is_purchase=True`` (compra): ``movement_type='purchase'`` + su COGS
      (``ExpenseEntry`` INVENTORY/COGS) cuando hay costo — como un libro de compras.

    En ambos casos se registra el ``InventoryMovement`` estampado con origen (A2) para
    traza/dedup/reversa. ``delta > 0`` = ingreso; ``delta < 0`` = baja (siempre ajuste,
    sin COGS); ``delta == 0`` = no-op.
    """
    if delta == 0:
        return
    # F6-C3: NO incluir la fecha en el hash de identidad de la fila de catálogo.
    # ``tx_date`` acá es SIEMPRE ``today`` sintético (el ancla catalog_initial_stock
    # no tiene fecha de negocio real — por eso inventory_temporal_service la ignora,
    # ver invariante 2d). Meter un valor sintético en un hash de IDENTIDAD LÓGICA es
    # incorrecto de principio: hacía que el MISMO catálogo, releído otro día, produjera
    # un ``source_row_hash`` distinto.
    # Alcance real del hash de catálogo (verificado en el review): su ÚNICO consumidor
    # es F3 (product_dedup_service), y ahí SOLO lo lee ``compute_group_fingerprint``
    # (detección de cambio dry-run↔apply de un grupo de duplicados). La decisión de
    # stock de un grupo catálogo-puro es ``STOCK_MOST_RECENT`` (max qty), que NO usa el
    # hash — así que esto NO previene ningún "doble conteo" (el dedup por hash con SUM
    # es exclusivo de COMPRAS, cuyo hash lo genera _apply_purchase_to_stock con la fecha
    # REAL del gasto, intacto). El beneficio concreto es acotado: estabilizar ese
    # fingerprint de grupo entre días. La identidad lógica queda producto+qty+costo+
    # proveedor+upload; el ``upload_id`` ya separa archivos distintos. NO se usa el ancla
    # de fila: incluye el índice y rompería la estabilidad ante reordenamiento del Excel.
    _row_hash = compute_source_row_hash(
        product_key=product_name,
        qty=delta,
        unit_cost=unit_cost,
        movement_date=None,
        supplier_key=store_name,
        upload_id=uploaded_file_id,
    )
    movement_type = "purchase" if (is_purchase and delta > 0) else "adjustment"
    await _record_stock_movement(
        session,
        tenant_id,
        product_id,
        delta,
        unit_cost,
        movement_type,
        final_qty,
        balance_index=balance_index,
        source_type=SOURCE_CATALOG_INITIAL_STOCK,
        source_upload_id=uploaded_file_id,
        source_row_ref=source_row_ref,
        source_row_hash=_row_hash,
        occurred_at=tx_date,
    )
    # Solo una COMPRA genera gasto de mercadería + baja de caja. El saldo de apertura
    # es un activo que el negocio ya tenía → no toca caja ni COGS.
    if is_purchase:
        _add_catalog_initial_stock_cogs(
            session,
            tenant_id,
            product_id=product_id,
            product_name=product_name,
            qty=delta,
            unit_cost=unit_cost,
            tx_date=tx_date,
            source_upload_id=uploaded_file_id,
            source_row_ref=source_row_ref,
        )


_NOMBRE_COLS: set[str] = {
    "producto",
    "descripcion",
    "descripción",
    "nombre",
    "articulo",
    "artículo",
    "item",
    "name",
    "concepto",
    "detalle",
    # FASE 3: columnas de compra de mercadería sirven como nombre del producto.
    "mercaderia",
    "mercadería",
    "insumo",
    "insumos",
}
_PRECIO_VENTA_COLS: set[str] = {
    "precio_venta", "precio", "price", "p_venta",
    "precio_unitario",  # common in Argentine business files
    "precio_unit",
}
_COSTO_COLS: set[str] = {
    "costo", "cost", "precio_costo", "p_costo",
    "costo_unitario",  # common in purchase sheets
    "costo_unit",
    # "precio de compra" / "compra+envio" → costo de adquisición unitario en
    # catálogos (decoración/retail AR). "compra" como substring cubre ambos.
    "precio_compra", "precio_de_compra", "p_compra", "costo_compra",
}
# Solo nombres INEQUÍVOCOS de costo unitario: "costo" a secas suele ser el
# total de la línea en archivos de gastos (también matchea _GASTO_COLS) y no
# debe escribirse como unit_cost del producto.
#
# "precio de compra" es el costo unitario en catálogos argentinos (caso real
# ASTERIA, vertical decoración). Las variantes multiword (precio_compra /
# precio_de_compra) son inequívocas y van en AMBOS paths (productos y gastos).
# El substring "compra" a secas SOLO se agrega al detector de costo del path de
# PRODUCTOS (ver _COSTO_UNITARIO_PRODUCT_COLS): en hojas de GASTOS una columna
# "compra" podría ser el TOTAL de la línea (libro de compras), así que el path de
# gastos se queda con las multiword narrow para no corromper el unit_cost.
_COSTO_UNITARIO_COLS: set[str] = {
    "costo_unitario", "costo_unit", "precio_costo", "p_costo", "unit_cost",
    "precio_compra", "precio_de_compra", "p_compra", "costo_compra",
}
# Path de PRODUCTOS (catálogo/stock): además de las multiword, "compra" a secas
# (cubre "precio_de_compra" y "compra+envio") es costo de adquisición. No se usa
# en el path de gastos para no confundir el total de un libro de compras con el
# costo unitario.
_COSTO_UNITARIO_PRODUCT_COLS: set[str] = _COSTO_UNITARIO_COLS | {"compra"}
_STOCK_COLS: set[str] = {
    "stock", "cantidad", "inventario", "units", "qty", "existencia", "stock_actual",
}
_SKU_COLS: set[str] = {"sku", "codigo", "código", "code", "ref", "id_producto"}
# F2-T5: columnas de código de barras (EAN/UPC/GTIN). Tokens distintivos para no
# colisionar con el "codigo" genérico de _SKU_COLS; "barras" alcanza (substring)
# para "codigo de barras"/"cod. barras". La colisión residual (un header que
# matchea ambos) se resuelve dando prioridad a barcode sobre sku en la detección.
_BARCODE_COLS: set[str] = {
    "barcode", "ean", "upc", "gtin", "barras", "cod_barra", "codigo_barra",
}
_PROVEEDOR_COLS: set[str] = {
    "proveedor",
    "proveedores",
    "proveedor_nombre",
    "nombre_proveedor",
    "empresa",
    "supplier",
    # Catálogos de productos (caso ASTERIA): la columna "Tienda" identifica el
    # comercio/proveedor de origen del artículo. Find-or-create de Supplier.
    # OJO: `_find_col` matchea por SUBSTRING, así que solo agregamos aliases
    # INEQUÍVOCOS. "local" queda AFUERA a propósito: matchearía "localidad",
    # "local_venta", etc. (falsos positivos). "tienda"/"tiendas"/"comercio"/
    # "negocio" son seguros.
    "tienda",
    "tiendas",
    "comercio",
    "negocio",
}

# Columnas de monto de venta ampliadas para archivos multi-hoja
_VENTA_AMOUNT_COLS: set[str] = _VENTA_COLS | {"total", "importe_total", "precio_unitario"}
# Columnas de monto de gasto ampliadas
_GASTO_AMOUNT_COLS: set[str] = _GASTO_COLS | {"monto", "total", "importe"}

# F6-B1/B2: fechas de producto. La genérica "fecha" queda AFUERA a propósito (no
# le roba la columna a la fecha de venta/gasto en hojas mixtas). Espejan las
# heurísticas de column_mapping_service._HEURISTICS["product"].
_ACQUIRED_COLS: set[str] = {
    "alta", "adquisicion", "adquisición", "fecha_alta", "fecha_ingreso", "fecha_compra",
}
_EXPIRY_COLS: set[str] = {
    "vencimiento", "caducidad", "vence", "vto", "expira", "expiracion", "expiración",
}


def _product_date_invalid_explicit(
    raw: Any, parsed: object, explicitly_mapped: bool
) -> bool:
    """F6-B2 (política de fecha inválida): True si el usuario MAPEÓ explícitamente el
    campo de fecha de producto y la celda trae un valor NO vacío que no parseó → la
    fila va a /otros (sin aplicarse parcialmente). Columna heurística o celda vacía
    NO cuentan (un producto es válido sin fecha)."""
    return (
        explicitly_mapped
        and raw is not None
        and str(raw).strip() != ""
        and parsed is None
    )


def _accumulate_acquired_at(
    existing: datetime | None, new: datetime | None
) -> datetime | None:
    """F6-B2: fecha de alta = la MÁS ANTIGUA conocida (un producto se dio de alta
    una vez; reimportes con otra fecha no la hacen "más nueva")."""
    candidates = [d for d in (existing, new) if d is not None]
    return min(candidates) if candidates else None


def _accumulate_expiry_date(
    existing: date | None, new: date | None, today: date
) -> date | None:
    """F6-B2: vencimiento a nivel producto (no por lote — FEFO real necesita
    inventory_lots, fuera de alcance). Si hay al menos un vencimiento FUTURO → el
    más próximo (lo que primero hay que vender/descartar). Si TODOS están vencidos
    → el más reciente. Nunca se descarta una fecha por ser pasada: B4 necesita poder
    mostrar "vencido", y un producto sin expiry es indistinguible de uno sin
    vencimiento conocido."""
    candidates = [d for d in (existing, new) if d is not None]
    if not candidates:
        return None
    future = [d for d in candidates if d >= today]
    return min(future) if future else max(candidates)


def _parse_amount(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    s = re.sub(r"[$\s]", "", str(raw).strip())
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = Decimal(s)
        if val <= 0:
            logger.debug("ingestion.parse.amount_discarded", raw=str(raw), reason="non_positive")
            return None
        return val
    except InvalidOperation:
        return None


# F6-C1: el parser vive en app/domain/date_parsing.py — es el mismo que usa el
# gate de calidad, así que ya no pueden discrepar sobre el mismo archivo.
_parse_date = parse_business_datetime


def _find_col(headers: list[str], keywords: set[str] | tuple[str, ...]) -> str | None:
    """Si `keywords` es tupla, se respeta su orden como PRIORIDAD (el primer
    keyword que matchee alguna columna gana, sin importar el orden de headers).
    Con set, gana la primera columna en orden de archivo (comportamiento legacy).
    """
    if isinstance(keywords, tuple):
        for k in keywords:
            for h in headers:
                if k in h.lower().strip().replace(" ", "_"):
                    return h
        return None
    for h in headers:
        norm = h.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return h
    return None


def _is_total_cost_col(col: str | None) -> bool:
    """¿El nombre de la columna apunta a un COSTO TOTAL de línea (no unitario)?"""
    if not col:
        return False
    norm = col.lower().strip().replace(" ", "_")
    return "costo_total" in norm or "total_costo" in norm


def _resolve_unit_cost_col(
    headers: list[str],
    amount_col: str | None,
    price_col: str | None,
) -> str | None:
    """Mejora C: columna de costo UNITARIO para crear productos, narrow-first.

    Preferir una columna inequívoca de costo unitario
    (``_COSTO_UNITARIO_PRODUCT_COLS`` — incluye "precio de compra"/"compra" para
    catálogos AR). Si no hay, caer a la broad (``_COSTO_COLS``, ej "costo") SOLO
    si esa columna no coincide con la del monto/precio (en archivos de gastos
    "costo" suele ser el total de la línea). NUNCA aceptar una columna de "costo
    total". Si no hay columna inequívoca → ``None`` (el producto nace
    ``requires_completion=True``).
    """
    narrow = _find_col(headers, _COSTO_UNITARIO_PRODUCT_COLS)
    if narrow and not _is_total_cost_col(narrow):
        return narrow
    broad = _find_col(headers, _COSTO_COLS)
    if (
        broad
        and broad not in (amount_col, price_col)
        and not _is_total_cost_col(broad)
    ):
        return broad
    return None


def _resolve_sale_price_col(
    headers: list[str],
    cost_col: str | None,
) -> str | None:
    """Columna de PRECIO DE VENTA, desambiguada de la de compra/costo.

    "Precio de compra" y "Precio de venta final" comparten el substring "precio";
    el genérico de ``_PRECIO_VENTA_COLS`` (legacy) tomaba la primera por orden de
    archivo y guardaba el COSTO como precio de venta (caso real ASTERIA). Prioridad
    explícita, primer match gana:

      1. header con "venta"  → "precio de venta final"
      2. header con "lista"  → "precio de lista"
      3. "precio_venta" / "p_venta"
      4. genérico "precio" / "price" EXCLUYENDO cualquier header de compra/costo
         (o igual a ``cost_col``) — así nunca se cae a "Precio de compra".

    ``cost_col`` debe resolverse ANTES para poder excluirlo.
    """
    def _norm(h: str) -> str:
        return h.lower().strip().replace(" ", "_")

    for keyword in ("venta", "lista", "precio_venta", "p_venta"):
        hit = _find_col(headers, {keyword})
        if hit:
            return hit
    # Genérico, excluyendo compra/costo y la columna de costo ya resuelta.
    for h in headers:
        norm = _norm(h)
        if "compra" in norm or "costo" in norm:
            continue
        if cost_col is not None and h == cost_col:
            continue
        if "precio" in norm or "price" in norm:
            return h
    return None


def _row_col(row: dict[str, Any], keywords: set[str] | tuple[str, ...]) -> str | None:
    """Como `_row_val` pero devuelve el NOMBRE de la columna que matchea.

    Necesario cuando hay que comparar identidad de columnas (ej: no usar la
    misma columna como monto del gasto Y costo unitario del producto).
    """
    if isinstance(keywords, tuple):
        for k in keywords:
            for key in row:
                if k in key.lower().strip().replace(" ", "_"):
                    return key
        return None
    for key in row:
        norm = key.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return key
    return None


def _row_val_categoria(row: dict[str, Any]) -> Any:
    """Valor de la columna de categoría por keyword, salteando columnas de pago.

    El keyword 'tipo' matchearía 'tipo_pago' por substring y la categoría
    terminaría siendo el método de pago — una columna de pago nunca es la
    categoría del gasto.
    """
    for k in _CATEGORIA_COLS:
        for key, val in row.items():
            norm = key.lower().strip().replace(" ", "_")
            if k in norm and "pago" not in norm and "payment" not in norm:
                return val
    return None


def _row_val(row: dict[str, Any], keywords: set[str] | tuple[str, ...]) -> Any:
    """Devuelve el valor de la primera columna de *esta* fila cuyo nombre matchea.

    A diferencia de `_find_col` (que resuelve una columna fija para todo el
    bucket a partir de la primera fila), esto resuelve por fila. Necesario en
    archivos multi-hoja donde varias hojas del mismo tipo pueden tener esquemas
    de columnas distintos: sin esto, las filas de la segunda hoja se descartaban
    en silencio porque la columna detectada no existía en sus keys.

    Con tupla, el orden de keywords es prioridad (ver `_find_col`).
    """
    if isinstance(keywords, tuple):
        for k in keywords:
            for key, val in row.items():
                if k in key.lower().strip().replace(" ", "_"):
                    return val
        return None
    for key, val in row.items():
        norm = key.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return val
    return None


def _resolve_target_cols(mapping: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Desde un mapeo explícito source_col→target, resuelve columnas por target canónico.

    Devuelve (target_to_col, custom_field_cols): el primero mapea campo canónico
    (amount, transaction_date, name, sale_price_ars, ...) → source_col; el segundo
    mapea cf_key → source_col para los targets `custom_field:{key}`. Ignora "ignore".
    """
    target_to_col: dict[str, str] = {}
    custom_field_cols: dict[str, str] = {}
    for src_col, target in mapping.items():
        if target == "ignore":
            continue
        if target.startswith("custom_field:"):
            custom_field_cols[target[len("custom_field:"):]] = src_col
        elif target not in target_to_col:
            target_to_col[target] = src_col
    return target_to_col, custom_field_cols


async def insert_confirmed_data(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None = None,
    return_details: bool = False,
    # `after["updated_at"]` de cada producto tocado. Cuesta un `session.get` +
    # `refresh` POR PRODUCTO, así que solo lo pide quien lo usa: el touched-since
    # check del undo de relectura. El ledger de reversa del borrado solo necesita
    # `product_id` + `action`, y en un catálogo de 1258 filas esto eran ~2500
    # round-trips extra colgados del confirm, que ya corre inline y síncrono.
    stamp_product_updated_at: bool = False,
    column_mappings: dict[str, str] | None = None,
    context_mappings: dict[str, dict[str, str]] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    context_entity: dict[str, str] | None = None,
    source: str = "ingestion",
    uploaded_file_id: uuid.UUID | None = None,
    stock_treatment: str | None = None,
) -> dict[str, Any]:
    """Importa datos confirmados y, al cerrar, rutea las ventas sin cliente a "Local".

    Wrapper sobre ``_insert_confirmed_data_impl`` (chokepoint único para TODOS los
    callers: ingestión, chat, reread, data-repair). Las funciones internas crean
    ``SaleEntry`` sin ``customer_id``; acá, tras un ``flush`` (prod corre con
    ``autoflush=False``, así que los inserts pendientes no se ven sin él), una sola
    query reasigna las huérfanas al sentinela "Local". Idempotente.
    """
    from app.application.services.customer_sentinel import (  # noqa: PLC0415
        assign_orphan_sales_to_local,
    )

    # F3-T3: chokepoint de bulk-import (crea productos/stock por fuera de
    # ProductRepository.save). Shared lock ANTES de mutar. No-op en SQLite.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)

    counts = await _insert_confirmed_data_impl(
        session,
        tenant_id,
        summary,
        confirmed_fields=confirmed_fields,
        return_details=return_details,
        stamp_product_updated_at=stamp_product_updated_at,
        column_mappings=column_mappings,
        context_mappings=context_mappings,
        context_confirmed=context_confirmed,
        context_entity=context_entity,
        source=source,
        uploaded_file_id=uploaded_file_id,
        stock_treatment=stock_treatment,
    )
    await session.flush()
    await assign_orphan_sales_to_local(session, tenant_id)
    return counts


async def _stamp_updated_at_on_product_details(
    session: AsyncSession, product_details: list[dict[str, Any]]
) -> None:
    """F9b (Task 6): completa ``after["updated_at"]`` de cada entrada de
    ``product_details`` — DEBE llamarse recién DESPUÉS de un ``session.flush()``.

    ``Product.updated_at`` tiene ``onupdate=func.now()`` (server-side): tras el
    UPDATE que este mismo flush acaba de emitir, el atributo queda expirado en
    memoria (sin ``eager_defaults``) — leerlo directo dispara un lazy-load
    síncrono que revienta con ``MissingGreenlet`` bajo ``AsyncSession`` (mismo
    hallazgo de la Task 5 sobre clientes/proveedores, ver
    ``_reread_master_entities._audit``). Por eso el ``session.refresh()`` acá,
    nunca inline en el loop de filas: mientras se procesan las filas
    (``autoflush=False`` de la sessionmaker) el UPDATE de un producto puede
    seguir sin flushear —leer ``updated_at`` ahí daría el valor viejo, no el que
    dejó esta relectura— o, si otra fila SÍ lo flusheó de rebote (el
    ``begin_nested()`` de ``add_product_or_reuse`` al crear OTRO producto
    flushea todo el session, no solo el que crea), el atributo ya estaría
    expirado. Este valor es lo que la Task 7 compara contra el ``updated_at``
    vivo del producto para el touched-since check del undo — no se agrega a
    ``before`` (no hace falta para restaurar, y capturarlo ahí temprano tendría
    el mismo problema de timing).

    Sale_price_ars/stock_units/sku/barcode/category/fechas NO tienen
    ``onupdate`` server-side — son seguros de leer en cualquier momento, por
    eso solo ``updated_at`` necesita este paso aparte.
    """
    from app.persistence.models.product import Product  # noqa: PLC0415

    for pd in product_details:
        product = await session.get(Product, uuid.UUID(pd["product_id"]))
        if product is None:
            continue
        await session.refresh(product)
        pd["after"]["updated_at"] = (
            product.updated_at.isoformat() if product.updated_at else None
        )


async def _insert_confirmed_data_impl(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None = None,
    return_details: bool = False,
    # `after["updated_at"]` de cada producto tocado. Cuesta un `session.get` +
    # `refresh` POR PRODUCTO, así que solo lo pide quien lo usa: el touched-since
    # check del undo de relectura. El ledger de reversa del borrado solo necesita
    # `product_id` + `action`, y en un catálogo de 1258 filas esto eran ~2500
    # round-trips extra colgados del confirm, que ya corre inline y síncrono.
    stamp_product_updated_at: bool = False,
    column_mappings: dict[str, str] | None = None,
    context_mappings: dict[str, dict[str, str]] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    context_entity: dict[str, str] | None = None,
    source: str = "ingestion",
    uploaded_file_id: uuid.UUID | None = None,
    stock_treatment: str | None = None,
) -> dict[str, Any]:
    """Parse parsed_summary_json and insert rows into sales/expense/product tables.

    When return_details=True, also returns product_details list with per-row
    action ('CREATED'|'UPDATED'), product_id, name, before/after snapshots.

    FASE F: las filas de `otros_detectados` que el usuario NO reasigna a un tipo
    importable se persisten en `unclassified_records` (bandeja "Otros") con
    ``source``/``uploaded_file_id`` — `counts["otros"]` las cuenta.
    """
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    confirmed_fields = confirmed_fields or default_confirmed_fields(summary)
    # Tratamiento del stock del catálogo (apertura vs compra). Prioridad: parámetro
    # explícito (confirm) → lo guardado en el summary (relectura preserva la elección)
    # → default apertura (no distorsiona caja). Ver STOCK_TREATMENT_*.
    _treatment = (
        stock_treatment
        or summary.get("stock_treatment")
        or STOCK_TREATMENT_OPENING_BALANCE
    )
    stock_is_purchase = _treatment == STOCK_TREATMENT_PURCHASE
    # Fallback de fecha para filas sin fecha: ahora (captura hora del import).
    today = now_ar_naive()
    counts: dict[str, Any] = {
        "ventas": 0,
        "gastos": 0,
        "productos": 0,
        "otros": 0,
        # Señales para el banner de avisos (human-in-the-loop) del frontend:
        # sin_proveedor: compras de mercadería sin proveedor → sentinela "No identificado".
        # sin_producto: compras sin producto detallado → Product incompleto (requires_completion).
        "sin_proveedor": 0,
        "sin_producto": 0,
        # F1 (hotfix puente): fila de producto ambigua (≥2 activos con el mismo
        # nombre normalizado) — NO se importa, NO se toca ningún existente.
        "productos_ambiguos": 0,
        # F7c: maestros importados dentro del confirm (orden maestro→transacción).
        # "clientes"/"proveedores" = creados + actualizados (alias plano, F7c,
        # mantenido por compatibilidad — ver el desglose F7d abajo).
        "clientes": 0,
        "proveedores": 0,
        # F7d: desglose de maestros por bucket del import service (F7b). Solo
        # needs_review/invalidos ameritan aviso — nunca se persisten.
        "clientes_creados": 0,
        "clientes_actualizados": 0,
        "clientes_needs_review": 0,
        "clientes_invalidos": 0,
        "proveedores_creados": 0,
        "proveedores_actualizados": 0,
        "proveedores_needs_review": 0,
        "proveedores_invalidos": 0,
        # F7d: taxonomía reconciliada de resolución de referencia por fila
        # transaccional (ventas→cliente, compras→proveedor) — única fuente,
        # reemplaza los "clientes_sin_identificar"/"proveedores_sin_identificar"
        # de F7c. "anonimo" (sin referencia — mostrador/sin dato) NUNCA amerita
        # revisión; solo "no_resuelto" (trae referencia pero no matcheó) avisa.
        "ventas_cliente_identificado": 0,
        "ventas_cliente_anonimo": 0,
        "ventas_cliente_no_resuelto": 0,
        "compras_proveedor_identificado": 0,
        "compras_proveedor_anonimo": 0,
        "compras_proveedor_no_resuelto": 0,
    }
    product_details: list[dict[str, Any]] = []
    file_type = summary.get("file_type", "spreadsheet")

    # F7c — paso 1/2 del orden maestro→transacción: clientes y proveedores ANTES
    # que cualquier venta/gasto, misma transacción. Así una venta de una hoja
    # posterior del MISMO archivo puede vincular a un cliente recién creado acá.
    await _import_master_entities(
        session,
        tenant_id,
        summary,
        confirmed_fields,
        context_mappings,
        context_confirmed,
        column_mappings,
        counts,
    )

    # Batch anti-N+1: precargar las huellas de import del tenant una sola vez
    # (solo si hay dedup activa, i.e. uploaded_file_id real). Evita un SELECT y un
    # savepoint por fila contra la DB en archivos grandes (relectura/import).
    seen_fp: set[str] | None = (
        await _load_import_fingerprints(session, tenant_id)
        if uploaded_file_id is not None
        else None
    )
    # Snapshot del estado precargado para persistir SOLO las huellas nuevas al final.
    _preloaded_fp: frozenset[str] | None = (
        frozenset(seen_fp) if seen_fp is not None else None
    )
    # Cache de productos por id (creados + tocados) para evitar un session.get por
    # fila al aplicar stock, y para que con autoflush=False los productos recién
    # creados reciban su stock (session.get no ve pendientes sin flush).
    _product_cache: dict[uuid.UUID, Any] = {}

    if file_type == "spreadsheet":
        inferred_type = summary.get("inferred_type", "general")

        # ── Archivos multi-hoja: delegar a helper que procesa cada tipo por separado ──
        if inferred_type == "mixed" or summary.get("multi_sheet"):
            counts = await _insert_multisheet_data(
                session=session,
                tenant_id=tenant_id,
                summary=summary,
                confirmed_fields=confirmed_fields,
                today=today,
                return_details=return_details,
                stamp_product_updated_at=stamp_product_updated_at,
                product_details=product_details,
                counts=counts,
                column_mappings=column_mappings,
                context_mappings=context_mappings,
                context_confirmed=context_confirmed,
                context_entity=context_entity,
                source=source,
                uploaded_file_id=uploaded_file_id,
                seen_fp=seen_fp,
                product_cache=_product_cache,
                stock_is_purchase=stock_is_purchase,
            )
            if seen_fp is not None and _preloaded_fp is not None:
                await _persist_import_fingerprints(
                    session, tenant_id, seen_fp - _preloaded_fp
                )
            return counts

        # Índice de identidad de clientes para resolver la referencia por fila en
        # ventas (F7c). Incluye los clientes recién creados por el paso maestro de
        # arriba (los import services flushean por fila, visibles en la sesión).
        _customer_identity_index: dict[IdentityKey, Any] = await _load_customer_identity_index(
            session, tenant_id
        )
        _local_sentinel_id: uuid.UUID | None = None

        async def _get_local_sentinel() -> uuid.UUID:
            nonlocal _local_sentinel_id
            if _local_sentinel_id is None:
                from app.application.services.customer_sentinel import (  # noqa: PLC0415
                    resolve_or_create_local_sentinel,
                )

                _local_sentinel_id = await resolve_or_create_local_sentinel(session, tenant_id)
            return _local_sentinel_id

        # F7c: modo de resolución de proveedor por fila en compras. "legacy"
        # (default) no cambia nada — el índice de identidad ni se carga.
        # "link_only" nunca crea proveedor desde una fila.
        _supplier_ref_mode = get_settings().SUPPLIER_REFERENCE_CREATION_MODE
        _supplier_identity_index: dict[IdentityKey, Any] = (
            await _load_supplier_identity_index(session, tenant_id)
            if _supplier_ref_mode == "link_only"
            else {}
        )

        rows: list[dict[str, Any]]
        rows_from_otros = False
        if inferred_type == "stock":
            rows = summary.get("stock_detectado", [])
        else:
            rows = summary.get("ventas_detectadas", []) or summary.get("gastos_detectados", [])
            if not rows:
                # FASE F: archivos ambiguos ("general") viven en otros_detectados;
                # la confirmación explícita del usuario sigue pudiendo importarlos.
                rows = summary.get("otros_detectados", [])
                rows_from_otros = bool(rows)
        if not rows:
            return counts

        headers = list(rows[0].keys())
        fecha_col = _find_col(headers, _FECHA_COLS)
        # Usar set ampliado para columnas de monto (ej: "precio_unitario", "total")
        venta_col = _find_col(headers, _VENTA_AMOUNT_COLS)
        gasto_col = _find_col(headers, _GASTO_AMOUNT_COLS)
        nombre_col = _find_col(headers, _NOMBRE_COLS)
        # Mejora C: costo unitario narrow-first. Se resuelve ANTES que el precio
        # de venta para poder excluirlo del precio (desambiguar compra vs venta).
        # Preferir una columna inequívoca de costo unitario; solo caer a la broad
        # ("costo") si NO coincide con la del monto. Nunca una de "costo total".
        costo_col = _resolve_unit_cost_col(headers, venta_col, None)
        # Precio de venta desambiguado: "venta" > "lista" > "precio_venta" >
        # genérico "precio" EXCLUYENDO compra/costo (y la columna de costo ya
        # resuelta). Antes el genérico "precio" tomaba "Precio de compra".
        precio_col = _resolve_sale_price_col(headers, costo_col)
        # Sin heurística: el precio de lista solo existe si el usuario lo mapeó
        # explícitamente (se resuelve más abajo, con el resto del mapeo).
        lista_col: str | None = None
        stock_col = _find_col(headers, _STOCK_COLS)
        # F2-T5: barcode ANTES que sku para poder desambiguar la colisión ("código
        # de barras" matchea también "codigo" de _SKU_COLS). Si el único header
        # tipo-código es el barcode, gana barcode (no es un SKU).
        barcode_col = _find_col(headers, _BARCODE_COLS)
        sku_col = _find_col(headers, _SKU_COLS)
        if sku_col is not None and sku_col == barcode_col:
            sku_col = None
        supplier_col = _find_col(headers, _PROVEEDOR_COLS)
        # F6-B1/B2: fechas de producto por heurística (la genérica "fecha" no cuenta).
        # El override explícito y los flags "mapeado a mano" se resuelven abajo.
        acquired_col = _find_col(headers, _ACQUIRED_COLS)
        expiry_col = _find_col(headers, _EXPIRY_COLS)
        _acquired_explicit = False
        _expiry_explicit = False

        # Columnas extra (solo disponibles con column_mappings explícitos)
        qty_col: str | None = None
        unit_price_col: str | None = None
        notes_col: str | None = None
        payment_col: str | None = None
        category_col: str | None = None
        recurring_col: str | None = None
        custom_field_cols: dict[str, str] = {}
        # F7c: declarado siempre (no solo dentro de `if column_mappings:`) para
        # que la resolución de referencia cliente/proveedor por fila pueda usar
        # `target_to_col.get(...)` sin guardas — vacío si no hay mapeo explícito
        # (la referencia es opt-in, ver `_customer_reference_record`).
        target_to_col: dict[str, str] = {}

        if column_mappings:
            # Construir lookup: target_field → primer source_col que lo mapee
            for src_col, target in column_mappings.items():
                if target == "ignore":
                    continue
                if target.startswith("custom_field:"):
                    cf_key = target[len("custom_field:"):]
                    custom_field_cols[cf_key] = src_col
                else:
                    if target not in target_to_col:
                        target_to_col[target] = src_col

            # Remapear columnas de fecha y monto usando el mapeo explícito
            fecha_col = (
                target_to_col.get("transaction_date")
                or target_to_col.get("expense_date")
                or fecha_col
            )
            if inferred_type != "stock" and "amount" in target_to_col:
                venta_col = target_to_col["amount"]
                gasto_col = target_to_col["amount"]
            nombre_col = (
                target_to_col.get("product_name")
                or target_to_col.get("name")
                or nombre_col
            )
            precio_col = target_to_col.get("sale_price_ars") or precio_col
            costo_col = target_to_col.get("unit_cost_ars") or costo_col
            stock_col = target_to_col.get("stock_units") or stock_col
            # Precio de lista (sugerido): SOLO por mapeo explícito. A diferencia de
            # `precio_col`/`costo_col` no tiene fallback heurístico a propósito —
            # si nadie declaró que una columna es el sugerido, el dato no existe y
            # queda NULL. Inventarlo desde un header parecido sería justamente lo
            # que rompió el import de ASTERIA.
            lista_col = target_to_col.get("list_price_ars")
            sku_col = target_to_col.get("sku") or sku_col
            barcode_col = target_to_col.get("barcode") or barcode_col
            if sku_col is not None and sku_col == barcode_col:
                sku_col = None
            supplier_col = target_to_col.get("supplier_name") or supplier_col

            # Campos extra solo disponibles con mapeo explícito
            qty_col = target_to_col.get("quantity")
            # Precio realmente vendido de ESTA fila. Solo por mapeo explícito y
            # sin fallback: nunca se deriva de amount/quantity (ver la nota en
            # models/transaction.py).
            unit_price_col = target_to_col.get("unit_price")
            notes_col = target_to_col.get("notes")
            payment_col = target_to_col.get("payment_method")
            category_col = target_to_col.get("category")
            recurring_col = target_to_col.get("is_recurring")
            # F6-B2: override explícito de fechas de producto + marca "mapeado a mano"
            # (necesaria para la política de fecha inválida → /otros).
            if "acquired_at" in target_to_col:
                acquired_col = target_to_col["acquired_at"]
                _acquired_explicit = True
            if "expiry_date" in target_to_col:
                expiry_col = target_to_col["expiry_date"]
                _expiry_explicit = True

        # FASE 3: en archivos ambiguos ("general") se honra la confirmación EXPLÍCITA del
        # usuario (no se requiere la señal auto-detectada). Para tipos ya inferidos se
        # mantiene la guardia original.
        wants_ventas = bool(
            inferred_type != "stock"
            and confirmed_fields.get("ventas")
            and (summary.get("has_venta") or inferred_type in ("ventas", "general"))
            and venta_col
        )
        wants_gastos = bool(
            inferred_type != "stock"
            and confirmed_fields.get("gastos")
            and (summary.get("has_gasto") or inferred_type in ("gastos", "general"))
            and gasto_col
        )
        wants_productos = bool(
            confirmed_fields.get("productos")
            and (summary.get("has_producto") or inferred_type == "stock")
            and nombre_col
        )

        # FASE 3: índice de catálogo en memoria para el LINK de ventas/gastos/compras
        # (tiers sku exacto → nombre → tokens, con SKU-que-gana-sobre-nombre). Una
        # sola carga; vacío si no hay columnas de nombre/sku.
        _by_sku, _by_name, _by_token = (
            await _load_product_index(session, tenant_id)
            if (wants_ventas or wants_gastos) and (nombre_col or sku_col)
            else ({}, {}, {})
        )
        # F2-T4/T5: índices de identidad + caché intra-corrida COMPARTIDOS por el
        # bucket de productos, la decisión de CREAR de compras (motor con detección
        # de ambiguo/conflicto → Otros) y el tier de BARCODE del link de
        # ventas/gastos (``_identity_indexes.by_barcode``). Una carga por corrida.
        _identity_indexes = (
            await _load_product_identity_indexes(session, tenant_id)
            if (wants_ventas or wants_gastos or wants_productos)
            else ProductIdentityIndexes({}, {}, {}, {}, {})
        )
        products_by_identity_key: dict[str, Product] = {}
        # Índice de proveedores para find-or-create en COMPRAS. Una sola carga;
        # vacío salvo en el path de compras (gastos con columna de proveedor) o
        # cuando hay que asignar el sentinela a compras de mercadería sin proveedor.
        # NO se materializa en el path de catálogo de productos: una marca/tienda
        # de un catálogo es atributo del producto (``custom_fields["marca"]``), no
        # un proveedor — el bug era crear Supplier desde ahí.
        _supplier_index: dict[str, uuid.UUID] = (
            await _load_supplier_index(session, tenant_id) if wants_gastos else {}
        )
        # FASE E: vertical del tenant para normalizar categorías de producto.
        # También para gastos: una categoría que matchea el catálogo de productos
        # del vertical es compra de mercadería → INVENTORY/COGS.
        _vertical = await _load_tenant_vertical(session, tenant_id)
        # Batch: balances del tenant en una query (evita un SELECT por fila
        # en los movimientos de stock del import).
        _balance_index: dict[uuid.UUID, Any] | None = (
            await _load_balance_index(session, tenant_id)
            if (wants_productos or wants_gastos)
            else None
        )
        # Traza agregada de decisiones de proveedor (Fase 1): proveedores reales
        # resueltos desde compras y si se usó el sentinela "No identificado".
        _real_suppliers: set[str] = set()
        _sentinel_used = False
        # A4 (guarda RC2): índices de fila ya procesados como COMPRA DE MERCADERÍA
        # en el bloque de gastos (crearon/repusieron producto + stock + COGS). El
        # bloque de productos los saltea: reprocesarlos duplicaría el producto
        # (autoflush=False oculta el pendiente al ``select``) y escribiría el stock
        # dos veces sobre las MISMAS filas. ``wants_gastos`` y ``wants_productos``
        # NO son mutuamente excluyentes y pueden correr sobre el mismo ``rows``.
        _merch_purchase_rows: set[int] = set()
        # Review F2 #1: filas de COMPRA ruteadas a "Otros" (producto ambiguo/en
        # conflicto). El bucket de productos también las saltea — si no, con
        # wants_gastos+wants_productos sobre las mismas rows, la fila se capturaría
        # a Otros dos veces. Set aparte de _merch_purchase_rows: acá NO se aplicó
        # una compra (no se creó producto ni stock), solo se difirió a revisión.
        _captured_to_otros_rows: set[int] = set()

        for row_index, row in enumerate(rows):
            # B1: idempotencia. Si esta fila (archivo+índice) ya se importó en una
            # corrida previa, saltarla (re-subir el mismo archivo = 0 filas nuevas).
            # Chequeo READ-ONLY: la huella se registra recién al final de la fila y
            # SOLO si insertó algún registro — una fila inválida no queda "quemada"
            # y puede reintentarse corregida. Solo aplica con uploaded_file_id real
            # (el correctivo de repair llama sin él y nunca dedupea acá).
            _row_anchor = (
                _import_row_anchor(tenant_id, uploaded_file_id, None, row_index)
                if uploaded_file_id is not None
                else None
            )
            if _row_anchor is not None and await _import_row_seen(
                session, tenant_id, _row_anchor, seen_fp
            ):
                continue
            _inserted_before = counts["ventas"] + counts["gastos"]
            # Review F2 #4/#6: resultado "captura a Otros" de esta fila. Se trata
            # como output persistido (registra fingerprint) sin usar `continue`,
            # para no saltear el bloque de idempotencia del final de la iteración.
            _captured_to_otros = False

            raw_date = row.get(fecha_col) if fecha_col else None
            tx_date = _parse_date(raw_date) if raw_date is not None else None
            # F6-A2: sin fecha reconocible NO se inventa "hoy" (invariante 2d). Si la
            # fila iba a registrar una operación fechada (venta/gasto con monto), va
            # a /otros para revisión manual — no se crea NADA desde ella (ni venta,
            # ni gasto, ni producto, ni proveedor). Filas sin monto (o de producto
            # puro, que no necesita fecha) NO se rutean: tx_date=None nunca llega a
            # un registro porque el `if amount:` interno de cada bloque lo protege.
            if tx_date is None:
                _venta_amount = (
                    _parse_amount(row.get(venta_col)) if (wants_ventas and venta_col) else None
                )
                _gasto_amount = (
                    _parse_amount(row.get(gasto_col)) if (wants_gastos and gasto_col) else None
                )
                if _venta_amount is not None or _gasto_amount is not None:
                    counts["otros"] += _capture_unclassified(
                        session,
                        tenant_id,
                        rows=[row],
                        headers=headers,
                        source=source,
                        uploaded_file_id=uploaded_file_id,
                        context_label=(
                            "Fila sin fecha reconocible: no se puede registrar la "
                            "operación sin inventar la fecha"
                        ),
                        suggested_entity="sale" if _venta_amount is not None else "expense",
                    )
                    _captured_to_otros_rows.add(row_index)
                    _captured_to_otros = True
                    logger.debug(
                        "ingestion.parse.date_row_routed_to_otros",
                        raw=str(raw_date),
                        row_index=row_index,
                    )

            if wants_ventas and not _captured_to_otros:
                assert venta_col is not None  # wants_ventas implica venta_col presente
                amount = _parse_amount(row.get(venta_col))
                if amount:
                    # Cantidad
                    qty_raw = row.get(qty_col) if qty_col else None
                    qty: int = 1
                    if qty_raw not in (None, "", "None", "nan"):
                        try:
                            qty = int(float(str(qty_raw)))
                        except (ValueError, TypeError):
                            qty = 1

                    # Notas
                    notes_raw = row.get(notes_col) if notes_col else None
                    notes_str = (
                        str(notes_raw).strip()[:499]
                        if notes_raw and str(notes_raw).strip() not in {"None", "nan", ""}
                        else "Importado desde archivo"
                    )

                    # Método de pago: canónico (antes se guardaba el texto crudo
                    # del archivo — "efectivo", "mercadopago" — y los filtros
                    # quedaban inconsistentes con los registros manuales).
                    pay_raw = _clean_str(
                        row.get(payment_col) if payment_col else _row_val(row, _PAGO_COLS),
                        50,
                    )
                    pay_str = normalize_payment_method(pay_raw) if pay_raw else "cash"

                    # Custom fields
                    cf: dict[str, str] = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }

                    entry = SaleEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        quantity=qty,
                        unit_price=(
                            _parse_amount(row.get(unit_price_col)) if unit_price_col else None
                        ),
                        transaction_date=tx_date,
                        payment_method=pay_str,
                        notes=notes_str,
                        provenance="REAL",
                        source_upload_id=uploaded_file_id,
                    )
                    # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
                    entry.product_id = _resolve_product(
                        _by_sku,
                        _by_name,
                        row.get(nombre_col) if nombre_col else None,
                        row.get(sku_col) if sku_col else None,
                        _by_token,
                        by_barcode=_identity_indexes.by_barcode,
                        barcode=row.get(barcode_col) if barcode_col else None,
                    )
                    # F7c: resolución de cliente por fila — matched/anonymous/
                    # unresolved. Nunca crea: solo vincula contra un cliente
                    # existente (maestro importado arriba o ya en la DB) o cae al
                    # sentinela "Local" con traza si la referencia no matchea.
                    _cust_ref = _classify_row_reference(
                        _customer_reference_record(row, target_to_col),
                        doc_fields=_CUSTOMER_DOC_FIELDS,
                        existing_index=_customer_identity_index,
                        anonymous_name_tokens=_ANONYMOUS_CUSTOMER_TOKENS,
                    )
                    if _cust_ref.outcome == "matched":
                        assert _cust_ref.entity is not None
                        entry.customer_id = _cust_ref.entity.id
                        cf["_customer_resolution"] = "matched"
                    else:
                        entry.customer_id = await _get_local_sentinel()
                        cf["_customer_resolution"] = _cust_ref.outcome
                        if _cust_ref.outcome == "unresolved" and _cust_ref.raw_value:
                            cf["_customer_reference_raw"] = _cust_ref.raw_value
                    _bump_reference_counts(counts, "ventas_cliente", _cust_ref.outcome)
                    if cf:
                        entry.custom_fields = cf
                    # Mejora D: trazabilidad import → fila origen.
                    if _row_anchor is not None:
                        entry.source_row_ref = _source_row_ref(_row_anchor)
                    session.add(entry)
                    counts["ventas"] += 1

            if wants_gastos and not _captured_to_otros:
                assert gasto_col is not None  # wants_gastos implica gasto_col presente
                amount = _parse_amount(row.get(gasto_col))
                if amount:
                    desc_raw = row.get(nombre_col) if nombre_col else None
                    notes_raw = row.get(notes_col) if notes_col else None
                    desc = (
                        str(notes_raw or desc_raw or "").strip()[:499]
                        or "Gasto importado"
                    )
                    # Categoría: columna mapeada explícita o detección por keyword
                    # (antes, sin mapeo explícito todo caía a "importado").
                    # _row_val_categoria saltea columnas de pago (tipo_pago ≠ categoría).
                    cat_raw = (
                        row.get(category_col)
                        if category_col
                        else _row_val_categoria(row)
                    )
                    # Categoría de producto del vertical ("Bebidas") = compra de
                    # mercadería → INVENTORY/COGS, preservando el texto como label.
                    cat_code, cat_label, _ = classify_expense_with_vertical(
                        _clean_str(cat_raw), _vertical
                    )

                    # Método de pago real del archivo (antes hardcodeado "transfer").
                    exp_pay_raw = _clean_str(
                        row.get(payment_col) if payment_col else _row_val(row, _PAGO_COLS),
                        30,
                    )
                    exp_pay = (
                        normalize_payment_method(exp_pay_raw) if exp_pay_raw else "transfer"
                    )
                    recurring = _parse_bool_es(
                        row.get(recurring_col) if recurring_col else _row_val(row, _RECURRENTE_COLS)
                    )

                    # Custom fields
                    cf = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }
                    if cat_label:
                        cf = {**cf, "category_label": cat_label}

                    expense = ExpenseEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        category=cat_code,
                        transaction_date=tx_date,
                        description=desc,
                        is_recurring=recurring if recurring is not None else False,
                        payment_method=exp_pay,
                        provenance="REAL",
                        source_upload_id=uploaded_file_id,
                    )
                    # Capturar proveedor real si la fila lo trae. F7c: gobernado por
                    # SUPPLIER_REFERENCE_CREATION_MODE. "legacy" (default) — sin
                    # cambios, find-or-create por nombre como siempre. "link_only" —
                    # matchea por identidad (CUIL/email/tel/nombre) y NUNCA crea; si
                    # no matchea, la decisión (sentinela o no) se difiere al bloque de
                    # abajo, que ya sabe si la fila es una compra de mercadería.
                    _pending_supplier_ref: RowReferenceResolution | None = None
                    # Review 7d (Important): el contador de "matched" se difiere al
                    # bloque `if not _captured_to_otros` (más abajo) — bumpearlo acá
                    # contaba una fila que después podía ir a "Otros" por producto
                    # ambiguo (el gasto nunca se persiste en ese caso).
                    _supplier_matched = False
                    if _supplier_ref_mode == "link_only":
                        _has_supplier_ref_col = bool(
                            supplier_col
                            or target_to_col.get("supplier_cuil")
                            or target_to_col.get("supplier_email")
                            or target_to_col.get("supplier_phone")
                        )
                        if _has_supplier_ref_col:
                            _sup_name_raw = row.get(supplier_col) if supplier_col else None
                            _sup_ref = _classify_row_reference(
                                _supplier_reference_record(row, target_to_col, _sup_name_raw),
                                doc_fields=_SUPPLIER_DOC_FIELDS,
                                existing_index=_supplier_identity_index,
                            )
                            if _sup_ref.outcome == "matched":
                                assert _sup_ref.entity is not None
                                expense.supplier_id = _sup_ref.entity.id
                                expense.supplier_name = _sup_ref.entity.name
                                cf["_supplier_resolution"] = "matched"
                                _supplier_matched = True
                            else:
                                _pending_supplier_ref = _sup_ref
                    elif supplier_col:
                        (
                            expense.supplier_id,
                            expense.supplier_name,
                        ) = await _resolve_or_create_supplier(
                            session,
                            tenant_id,
                            row.get(supplier_col),
                            _supplier_index,
                        )
                        if expense.supplier_name:
                            _real_suppliers.add(expense.supplier_name)
                    # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
                    _exp_name = str(row.get(nombre_col)) if nombre_col else None
                    _exp_sku = str(row.get(sku_col)) if sku_col else None
                    _exp_barcode = row.get(barcode_col) if barcode_col else None
                    expense.product_id = _resolve_product(
                        _by_sku,
                        _by_name,
                        _exp_name,
                        _exp_sku,
                        _by_token,
                        by_barcode=_identity_indexes.by_barcode,
                        barcode=_exp_barcode,
                    )
                    # FASE D: COGS si la fila es compra de mercadería (producto
                    # del catálogo o categoría INVENTORY); además suma stock si
                    # trae cantidad explícita.
                    exp_qty_raw = (
                        row.get(qty_col) if qty_col else _row_val(row, _CANTIDAD_COLS)
                    )
                    # Costo unitario: solo de una columna inequívoca y DISTINTA
                    # de la del monto — "costo" suele ser el total de la línea y
                    # escribirlo como unit_cost corrompería el margen.
                    exp_cost_col = (
                        target_to_col.get("unit_cost_ars")
                        if column_mappings
                        else _find_col(headers, _COSTO_UNITARIO_COLS)
                    )
                    exp_unit_cost = (
                        _parse_amount(row.get(exp_cost_col))
                        if exp_cost_col and exp_cost_col != gasto_col
                        else None
                    )
                    # Compra de mercadería = gasto COGS+caja Y alta/reposición de
                    # producto. Señal a nivel de fila: nombre de producto + cantidad>0
                    # (un libro de compras con esas columnas). Se CREA el producto
                    # ANTES de inferir expense_type para que un producto NUEVO (no en
                    # catálogo, sin categoría) igual quede COGS por su product_id — el
                    # orden inverso dejaba los productos nuevos como OPEX y nunca se
                    # creaban. Gate por (nombre + cantidad>0) o categoría INVENTORY:
                    # los gastos de servicio/alquiler (sin cantidad) NO crean producto.
                    _has_qty = _parse_qty(exp_qty_raw) > 0
                    _is_merch_purchase = bool(_clean_str(_exp_name, 299)) and _has_qty
                    if expense.product_id is None and (
                        _is_merch_purchase or (cat_code == "INVENTORY" and _has_qty)
                    ):
                        _action, _pid, _cands = await _resolve_purchase_identity(
                            session,
                            tenant_id,
                            name=_exp_name,
                            sku=_exp_sku,
                            brand=None,
                            barcode=_exp_barcode,
                            unit_cost=exp_unit_cost,
                            indexes=_identity_indexes,
                            cache=products_by_identity_key,
                            product_cache=_product_cache,
                        )
                        if _action == "otros":
                            # Review F2 #1/#3: compra con producto ambiguo/en conflicto
                            # NO crea un 3er producto — la fila va a "Otros" (con
                            # match_candidates), el gasto NO se registra, y se marca la
                            # fila para que el bucket de productos NO la recapture.
                            counts["otros"] += _capture_unclassified(
                                session,
                                tenant_id,
                                rows=[row],
                                headers=headers,
                                source=source,
                                uploaded_file_id=uploaded_file_id,
                                context_label="Compra de producto ambiguo: coincide "
                                "con varios productos del catálogo",
                                suggested_entity="expense",
                                match_candidates=_cands,
                            )
                            _captured_to_otros_rows.add(row_index)
                            _captured_to_otros = True
                        else:
                            expense.product_id = _pid
                            # Review F2 #3: solo "created" creó un producto incompleto;
                            # "linked" reusó uno ya creado en la corrida (no cuenta).
                            if _action == "created":
                                counts["sin_producto"] += 1
                            # Review F2 #2: registrar en los índices transaccionales
                            # (los de _resolve_product) para que ventas/gastos
                            # POSTERIORES del mismo archivo puedan vincularlo.
                            if _pid is not None:
                                _register_product_transaction_indexes(
                                    _pid, _exp_name, _exp_sku, _by_sku, _by_name, _by_token
                                )
                    # Review F2 #4: la cola de "aplicar el gasto" se saltea SIN
                    # `continue` (así el bloque de fingerprint del final igual corre).
                    if not _captured_to_otros:
                        # FASE D: COGS si la fila es compra de mercadería (producto del
                        # catálogo/recién creado o categoría INVENTORY); suma stock si
                        # trae cantidad explícita.
                        expense.expense_type = infer_expense_type(
                            cat_code, product_id=expense.product_id
                        )
                        # Sentinela: una compra de mercadería (tiene product_id) SIN
                        # proveedor informado se agrupa en "No identificado" — UNO por
                        # tenant. NO se aplica a OPEX (gastos operativos sin producto).
                        if expense.supplier_id is None and expense.product_id is not None:
                            expense.supplier_id = await _resolve_or_create_sentinel_supplier(
                                session, tenant_id, _supplier_index
                            )
                            _sentinel_used = True
                            counts["sin_proveedor"] += 1
                            # F7c (link_only): recién acá se sabe que es compra de
                            # mercadería — se aplica la clasificación diferida
                            # (anonymous/unresolved) que quedó pendiente arriba. En
                            # OPEX (product_id None) el sentinela nunca se toca, así
                            # que una referencia sin matchear en un gasto operativo
                            # queda sin proveedor y sin traza — igual que hoy.
                            if _supplier_ref_mode == "link_only":
                                _outcome = (
                                    _pending_supplier_ref.outcome
                                    if _pending_supplier_ref is not None
                                    else "anonymous"
                                )
                                cf["_supplier_resolution"] = _outcome
                                if _outcome == "unresolved":
                                    _raw = (
                                        _pending_supplier_ref.raw_value
                                        if _pending_supplier_ref
                                        else None
                                    )
                                    if _raw:
                                        cf["_supplier_reference_raw"] = _raw
                                _bump_reference_counts(counts, "compras_proveedor", _outcome)
                        elif _supplier_matched:
                            # Review 7d (Important): recién acá se sabe que la fila
                            # no fue descartada a "Otros" — bumpear antes hubiera
                            # contado una compra que nunca se persistió.
                            _bump_reference_counts(counts, "compras_proveedor", "matched")
                        if cf:
                            expense.custom_fields = cf
                        await _apply_purchase_to_stock(
                            session,
                            tenant_id,
                            expense,
                            exp_qty_raw,
                            exp_unit_cost,
                            balance_index=_balance_index,
                            product_cache=_product_cache,
                            source_row_ref=_source_row_ref(_row_anchor),
                        )
                        # A4 (RC2): si esta fila fue una compra de mercadería que aplicó
                        # stock (producto ligado + cantidad>0), marcarla para que el
                        # bloque de productos NO la reprocese (evita producto duplicado
                        # y doble escritura de stock sobre la misma fila).
                        if expense.product_id is not None and _has_qty:
                            _merch_purchase_rows.add(row_index)
                        # Mejora D: trazabilidad import → fila origen.
                        if _row_anchor is not None:
                            expense.source_row_ref = _source_row_ref(_row_anchor)
                        session.add(expense)
                        counts["gastos"] += 1

            # B1: registrar la huella si la fila produjo output (venta/gasto O
            # captura a Otros — review F2 #6: una captura a Otros es un resultado
            # PERSISTIDO; sin esto, re-subir el archivo re-crea el UnclassifiedRecord).
            # Si no produjo nada (sin monto parseable, mapeo incorrecto) NO se quema
            # y podrá reintentarse corregida.
            if _row_anchor is not None and (
                counts["ventas"] + counts["gastos"] > _inserted_before
                or _captured_to_otros
            ):
                await _register_import_row_fingerprint(
                    session, tenant_id, _row_anchor, seen_fp
                )

        # Traza agregada de las decisiones de proveedor del path de compras.
        if _real_suppliers:
            _audit_supplier_decision(
                session,
                tenant_id,
                decision_type="SUPPLIER_CREATED_FROM_PURCHASE",
                data={"suppliers": sorted(_real_suppliers), "count": len(_real_suppliers)},
            )
        if _sentinel_used:
            _audit_supplier_decision(
                session,
                tenant_id,
                decision_type="SUPPLIER_SENTINEL_CREATED",
                data={"name": _SENTINEL_SUPPLIER_NAME},
            )

        if wants_productos:
            assert nombre_col is not None  # wants_productos implica nombre_col presente
            _skipped_brands: set[str] = set()
            # F2-T4/T5: ``_identity_indexes`` y ``products_by_identity_key`` ya se
            # cargaron/crearon hoisteados arriba (compartidos con el link de
            # ventas/gastos/compras — motor unificado). La caché de identidad es la
            # MISMA: un producto creado por una compra en el bloque de gastos ya
            # quedó registrado y una fila de catálogo posterior lo reusa.
            for _prod_index, row in enumerate(rows):
                # F6-B2: idempotencia de las filas capturadas a /otros (fecha ilegible
                # mapeada a mano o identidad ambigua/en conflicto), con ancla propia
                # "producto". Los productos normales no fingerprintean (dedupean por
                # identidad/upsert); esta huella solo la registran las capturas, así una
                # relectura del archivo no re-crea el UnclassifiedRecord. READ-ONLY acá;
                # se registra recién al capturar.
                _prod_capture_anchor = (
                    _import_row_anchor(tenant_id, uploaded_file_id, "producto", _prod_index)
                    if uploaded_file_id is not None
                    else None
                )
                if _prod_capture_anchor is not None and await _import_row_seen(
                    session, tenant_id, _prod_capture_anchor, seen_fp
                ):
                    continue
                # A4 (RC2): fila ya importada como compra de mercadería en el bloque
                # de gastos (creó producto + stock + COGS) → reprocesarla duplicaría
                # producto/stock. Review F2 #1: o ya fue capturada a "Otros" por
                # ambigüedad en compras → recapturarla acá crearía un 2º
                # UnclassifiedRecord para la misma fila. En ambos casos se saltea.
                if (
                    _prod_index in _merch_purchase_rows
                    or _prod_index in _captured_to_otros_rows
                ):
                    continue
                name = str(row.get(nombre_col, "")).strip()[:299]
                if not name or name.lower() in {"none", "nan", ""}:
                    continue
                # F6-B2: fechas de producto (columna mapeada o heurística; la genérica
                # "fecha" no cuenta). Política de inválida: si un campo MAPEADO A MANO
                # trae valor no vacío ilegible → la fila va a /otros SIN aplicarse (no
                # se toca stock, precio ni identidad). acquired_at naive; expiry date.
                _acq_raw = row.get(acquired_col) if acquired_col else None
                _acquired = parse_business_datetime(_acq_raw) if _acq_raw is not None else None
                if _acquired is not None and _acquired.tzinfo is not None:
                    _acquired = _acquired.replace(tzinfo=None)
                _exp_raw = row.get(expiry_col) if expiry_col else None
                _expiry = parse_business_date(_exp_raw) if _exp_raw is not None else None
                if _product_date_invalid_explicit(
                    _acq_raw, _acquired, _acquired_explicit
                ) or _product_date_invalid_explicit(_exp_raw, _expiry, _expiry_explicit):
                    counts["otros"] += _capture_unclassified(
                        session,
                        tenant_id,
                        rows=[row],
                        headers=headers,
                        source=source,
                        uploaded_file_id=uploaded_file_id,
                        context_label=(
                            "Fecha de producto ilegible en una columna que mapeaste a "
                            "mano: revisá y completá antes de importar"
                        ),
                        suggested_entity="product",
                    )
                    if _prod_capture_anchor is not None:
                        await _register_import_row_fingerprint(
                            session, tenant_id, _prod_capture_anchor, seen_fp
                        )
                    continue
                # La columna "Tienda"/"proveedor" de un CATÁLOGO es marca/origen del
                # artículo, NO un proveedor: se guarda como atributo del producto en
                # ``custom_fields["marca"]``. Antes se creaba un Supplier por cada
                # marca (BIC, Tulipán, "importado"...) ensuciando Proveedores. Ya NO.
                store_name: str | None = (
                    _clean_str(row.get(supplier_col), 300) if supplier_col else None
                )
                if store_name:
                    _skipped_brands.add(store_name)
                # Mejora D: ref de fila origen para el producto creado (no para el
                # update de uno existente — ese conserva su ref original).
                _prod_row_ref = (
                    _source_row_ref(
                        _import_row_anchor(
                            tenant_id, uploaded_file_id, None, _prod_index
                        )
                    )
                    if uploaded_file_id is not None
                    else None
                )
                price = _parse_amount(row.get(precio_col)) if precio_col else None
                cost = _parse_amount(row.get(costo_col)) if costo_col else None
                list_price = _parse_amount(row.get(lista_col)) if lista_col else None
                try:
                    stock_raw = row.get(stock_col) if stock_col else None
                    stock_val = (
                        int(float(str(stock_raw)))
                        if stock_raw not in (None, "", "None", "nan")
                        else 0
                    )
                except (ValueError, TypeError):
                    stock_val = 0
                sku_raw = row.get(sku_col) if sku_col else None
                sku = (
                    str(sku_raw).strip()[:99]
                    if sku_raw and str(sku_raw).strip() not in {"", "None", "nan"}
                    else None
                )
                # F2-T5: código de barras de la fila (si el archivo trae la columna).
                barcode = _clean_str(row.get(barcode_col), 64) if barcode_col else None
                # FASE E: categoría canónica del vertical (antes se ignoraba en
                # este path). Sin columna de categoría → None (sin categoría).
                prod_cat_raw = _clean_str(
                    row.get(category_col) if category_col else _row_val_categoria(row),
                    99,
                )
                prod_cat: str | None = None
                prod_cat_label: str | None = None
                if prod_cat_raw:
                    prod_cat, prod_cat_label = normalize_product_category(
                        prod_cat_raw, _vertical
                    )

                # F2-T2: resolución de identidad por claves independientes
                # (barcode→sku→nombre+marca). Caché intra-corrida ANTES del
                # motor — evita duplicar con autoflush=False cuando 2 filas
                # del archivo comparten identidad (mismo patrón que F1).
                _sku_n = normalize_sku(sku)
                _name_n = normalize_product_name(name)
                _brand_n = normalize_brand(store_name)
                _bc_n = normalize_barcode(barcode)
                async def _merge_catalog_into_existing(
                    existing: Product,
                    *,
                    name: str = name,
                    sku: str | None = sku,
                    barcode: str | None = barcode,
                    price: Decimal | None = price,
                    cost: Decimal | None = cost,
                    list_price: Decimal | None = list_price,
                    stock_val: int = stock_val,
                    prod_cat: str | None = prod_cat,
                    store_name: str | None = store_name,
                    _prod_row_ref: str | None = _prod_row_ref,
                    _sku_n: str | None = _sku_n,
                    _name_n: str = _name_n,
                    _brand_n: str | None = _brand_n,
                    _bc_n: str | None = _bc_n,
                    _acquired: datetime | None = _acquired,
                    _expiry: date | None = _expiry,
                ) -> None:
                    """Aplica la fila del catálogo a un producto que YA existe.

                    Extraído en F5-A: ahora llegan acá dos caminos —el motor resolvió
                    el producto, o el índice único rechazó la creación y
                    ``add_product_or_reuse`` devolvió al ocupante—. Duplicar el cuerpo
                    era el riesgo real: en creación ``_apply_catalog_stock`` recibe
                    ``delta=stock_val`` (desde 0); aplicárselo a un producto que ya
                    tiene stock se lo SUMARÍA de nuevo.

                    Los defaults capturan los locales de ESTA iteración (un closure a
                    secas los leería tarde, ya pisados por la fila siguiente).
                    """
                    before_snap: dict[str, Any] | None = None
                    if return_details:
                        # F9b (Task 6): antes solo antes/after de precio+stock (stock
                        # SOLO informativo — el undo nunca lo restaura desde acá, ver
                        # invariante 2d). Ampliado a los demás campos mutables no-stock
                        # que esta misma función puede pisar más abajo, para que el
                        # undo (Task 7) tenga qué restaurar. Todos son columnas
                        # client-side (sin `onupdate` server-side) — seguro leerlas en
                        # cualquier momento, a diferencia de `updated_at` (ver el
                        # refresh post-flush más abajo en esta función).
                        before_snap = {
                            "sale_price_ars": str(existing.sale_price_ars),
                            # Revisión final F9b (Hallazgo 1): `unit_cost_ars` NO está
                            # cubierto por el mecanismo incremental de movimientos
                            # (ese solo ajusta stock_units/current_qty) — a diferencia
                            # de stock_units, si no se snapshotea acá el undo lo deja
                            # permanentemente en lo que decía el archivo releído.
                            "unit_cost_ars": (
                                str(existing.unit_cost_ars)
                                if existing.unit_cost_ars is not None
                                else None
                            ),
                            # Mismo motivo que unit_cost_ars: lo pisa esta función y
                            # no lo cubre ningún mecanismo incremental.
                            "list_price_ars": (
                                str(existing.list_price_ars)
                                if existing.list_price_ars is not None
                                else None
                            ),
                            "stock_units": existing.stock_units,
                            "sku": existing.sku,
                            "barcode": existing.barcode,
                            "category": existing.category,
                            "acquired_at": (
                                existing.acquired_at.isoformat()
                                if existing.acquired_at
                                else None
                            ),
                            "expiry_date": (
                                existing.expiry_date.isoformat()
                                if existing.expiry_date
                                else None
                            ),
                        }
                    if price:
                        existing.sale_price_ars = price
                    if cost:
                        existing.unit_cost_ars = cost
                    if list_price:
                        existing.list_price_ars = list_price
                    if stock_val > 0:
                        _delta = stock_val - existing.stock_units
                        existing.stock_units = stock_val
                        # FASE 3 + A2/A5: audit del cambio de stock + sync de balance;
                        # y si entró stock real (_delta>0) con costo, su COGS.
                        await _apply_catalog_stock(
                            session,
                            tenant_id,
                            product_id=existing.id,
                            product_name=name,
                            delta=_delta,
                            final_qty=stock_val,
                            unit_cost=cost,
                            store_name=store_name,
                            tx_date=today,
                            uploaded_file_id=uploaded_file_id,
                            source_row_ref=_prod_row_ref,
                            balance_index=_balance_index,
                            is_purchase=stock_is_purchase,
                        )
                    if sku:
                        existing.sku = sku
                    # F2-T5: completar barcode solo si el producto no tenía (no pisar
                    # un barcode ya cargado con el de una fila de reposición).
                    if barcode and not existing.barcode:
                        existing.barcode = barcode
                    if prod_cat and not existing.category:
                        existing.category = prod_cat
                    # F6-B2: acumular fechas de producto salvo edición manual del
                    # usuario. acquired_at = la más antigua; expiry_date por la regla
                    # futuro-más-próximo / vencido-más-reciente.
                    if not existing.has_user_edits:
                        _acc_acq = _accumulate_acquired_at(existing.acquired_at, _acquired)
                        if _acc_acq is not None:
                            existing.acquired_at = _acc_acq
                        _acc_exp = _accumulate_expiry_date(
                            existing.expiry_date, _expiry, today.date()
                        )
                        if _acc_exp is not None:
                            existing.expiry_date = _acc_exp
                    _register_product_identity_cache(
                        products_by_identity_key, existing, _sku_n, _name_n, _brand_n, _bc_n
                    )
                    if return_details:
                        product_details.append(
                            {
                                "action": "UPDATED",
                                "product_id": str(existing.id),
                                "name": name,
                                "before": before_snap,
                                "after": {
                                    "sale_price_ars": str(price or existing.sale_price_ars),
                                    "list_price_ars": (
                                        str(list_price or existing.list_price_ars)
                                        if (list_price or existing.list_price_ars) is not None
                                        else None
                                    ),
                                    "unit_cost_ars": (
                                        str(cost or existing.unit_cost_ars)
                                        if (cost or existing.unit_cost_ars) is not None
                                        else None
                                    ),
                                    "stock_units": stock_val or existing.stock_units,
                                    "sku": existing.sku,
                                    "barcode": existing.barcode,
                                    "category": existing.category,
                                    "acquired_at": (
                                        existing.acquired_at.isoformat()
                                        if existing.acquired_at
                                        else None
                                    ),
                                    "expiry_date": (
                                        existing.expiry_date.isoformat()
                                        if existing.expiry_date
                                        else None
                                    ),
                                },
                            }
                        )

                existing = _lookup_product_identity_cache(
                    products_by_identity_key, _sku_n, _name_n, _brand_n, _bc_n
                )
                if existing is None:
                    _resolution = _resolve_product_identity(
                        name, sku, store_name, indexes=_identity_indexes, barcode=barcode
                    )
                    if _resolution.status in ("ambiguous", "conflict"):
                        counts["productos_ambiguos"] += 1
                        logger.warning(
                            "ingestion.product_name_ambiguous",
                            tenant_id=str(tenant_id),
                            normalized_name=_name_n,
                            row_ref=_prod_row_ref,
                            uploaded_file_id=(
                                str(uploaded_file_id) if uploaded_file_id else None
                            ),
                            count=len(_resolution.candidates),
                            candidate_ids=[c["id"] for c in _resolution.candidates],
                            match_strategy=_resolution.status,
                        )
                        # La fila ambigua/en conflicto NO se descarta en
                        # silencio — queda en "Otros" (bandeja /otros) para
                        # revisión/unificación manual, con match_candidates.
                        _context_label = (
                            f"Producto ambiguo: coincide con {len(_resolution.candidates)} "
                            "productos activos"
                            if _resolution.status == "ambiguous"
                            else "Conflicto de identidad: el SKU y el nombre "
                            "apuntan a productos distintos"
                        )
                        counts["otros"] += _capture_unclassified(
                            session,
                            tenant_id,
                            rows=[row],
                            headers=headers,
                            source=source,
                            uploaded_file_id=uploaded_file_id,
                            context_label=_context_label,
                            suggested_entity="product",
                            match_candidates=_resolution.candidates,
                        )
                        # F6-B (review): la captura ambigua también es output
                        # PERSISTIDO → registrar la huella para que una relectura no
                        # re-cree el UnclassifiedRecord (paridad con multi-context, que
                        # ya lo hace vía el bool de _add_product). Antes solo la
                        # captura por fecha registraba; la ambigua re-capturaba (F2).
                        if _prod_capture_anchor is not None:
                            await _register_import_row_fingerprint(
                                session, tenant_id, _prod_capture_anchor, seen_fp
                            )
                        continue  # ambiguo/conflicto: no se importa, no se toca nada
                    if _resolution.status == "resolved" and _resolution.product_id is not None:
                        existing = await session.get(Product, _resolution.product_id)
                if existing:
                    await _merge_catalog_into_existing(existing)
                else:
                    new_product_id = uuid.uuid4()
                    cf_product: dict[str, str] = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }
                    if prod_cat_label:
                        cf_product = {**cf_product, "category_label": prod_cat_label}
                    if store_name:
                        cf_product = {**cf_product, "marca": store_name}
                    new_product = Product(
                        id=new_product_id,
                        tenant_id=tenant_id,
                        name=name,
                        # FASE 3 (B2): precio default 0 explícito para auto-creados incompletos.
                        sale_price_ars=price or Decimal("0"),
                        list_price_ars=list_price,
                        sku=sku,
                        barcode=barcode,  # F2-T5
                        unit_cost_ars=cost,
                        stock_units=stock_val,
                        category=prod_cat,
                        # NULL = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS del servidor
                        low_stock_threshold_units=None,
                        provenance="REAL",
                        # FASE 3 (B2): falta precio o costo → el usuario debe completarlo.
                        requires_completion=not price or not cost,
                        # F6-B2: fechas del archivo (None si no se mapearon/detectaron).
                        acquired_at=_acquired,
                        expiry_date=_expiry,
                        # `{}` y no `None`: la columna es JSONB nullable=False, pero
                        # un `None` explícito persiste como JSON `null` ('null'::jsonb)
                        # y rompe `GET /products` (ProductResponse exige dict → 503).
                        custom_fields=cf_product if cf_product else {},
                        source_row_ref=_prod_row_ref,  # Mejora D
                    )
                    # F5-A: sin ``session.add`` — el helper exige el objeto TRANSIENT
                    # para emitir el INSERT dentro del savepoint.
                    try:
                        _resolved, _created = await add_product_or_reuse(
                            session, new_product
                        )
                    except ProductIdentityConflictError as _conflict:
                        counts["productos_ambiguos"] += 1
                        logger.warning(
                            "ingestion.product_identity_ambiguous_on_insert",
                            tenant_id=str(tenant_id),
                            normalized_name=_name_n,
                            row_ref=_prod_row_ref,
                            matched_by=_conflict.matched_by,
                            candidate_ids=[str(p.id) for p in _conflict.candidates],
                        )
                        counts["otros"] += _capture_unclassified(
                            session,
                            tenant_id,
                            rows=[row],
                            headers=headers,
                            source=source,
                            uploaded_file_id=uploaded_file_id,
                            context_label="Conflicto de identidad: el código de barras "
                            "y el SKU apuntan a productos distintos",
                            suggested_entity="product",
                            match_candidates=_candidates_from_conflict(_conflict),
                        )
                        # F6-B (review): idempotencia de la captura (ver arriba).
                        if _prod_capture_anchor is not None:
                            await _register_import_row_fingerprint(
                                session, tenant_id, _prod_capture_anchor, seen_fp
                            )
                        continue  # ambiguo: no se importa, no se toca nada
                    if not _created:
                        # El índice único resolvió una carrera: es exactamente el
                        # camino de "producto existente", con su delta relativo.
                        await _merge_catalog_into_existing(_resolved)
                        counts["productos"] += 1
                        continue
                    _register_product_identity_cache(
                        products_by_identity_key, new_product, _sku_n, _name_n, _brand_n, _bc_n
                    )
                    # FASE 3 + A2/A5: audit del ingreso inicial de stock + balance +,
                    # si trae stock con costo, su COGS (stock inicial = compra real).
                    await _apply_catalog_stock(
                        session,
                        tenant_id,
                        product_id=new_product_id,
                        product_name=name,
                        delta=stock_val,
                        final_qty=stock_val,
                        unit_cost=cost,
                        store_name=store_name,
                        tx_date=today,
                        uploaded_file_id=uploaded_file_id,
                        source_row_ref=_prod_row_ref,
                        balance_index=_balance_index,
                        is_purchase=stock_is_purchase,
                    )
                    if return_details:
                        product_details.append(
                            {
                                "action": "CREATED",
                                "product_id": str(new_product_id),
                                "name": name,
                                "before": None,
                                "after": {
                                    "sale_price_ars": str(price or Decimal("0")),
                                    "list_price_ars": (
                                        str(new_product.list_price_ars)
                                        if new_product.list_price_ars is not None
                                        else None
                                    ),
                                    "unit_cost_ars": (
                                        str(new_product.unit_cost_ars)
                                        if new_product.unit_cost_ars is not None
                                        else None
                                    ),
                                    "stock_units": stock_val,
                                    "sku": new_product.sku,
                                    "barcode": new_product.barcode,
                                    "category": new_product.category,
                                    "acquired_at": (
                                        new_product.acquired_at.isoformat()
                                        if new_product.acquired_at
                                        else None
                                    ),
                                    "expiry_date": (
                                        new_product.expiry_date.isoformat()
                                        if new_product.expiry_date
                                        else None
                                    ),
                                },
                            }
                        )
                counts["productos"] += 1

            # Traza agregada: marcas de catálogo que ANTES se creaban como Supplier
            # y ahora son atributo del producto (no se creó ningún Supplier).
            if _skipped_brands:
                _audit_supplier_decision(
                    session,
                    tenant_id,
                    decision_type="SUPPLIER_SKIPPED_FROM_CATALOG",
                    data={
                        "skipped_brands": sorted(_skipped_brands),
                        "count": len(_skipped_brands),
                    },
                )

        # FASE F: filas ambiguas (otros_detectados) que el usuario NO reasignó a
        # ningún tipo importable → bandeja "Otros" en vez de descartarse.
        if rows_from_otros and not (wants_ventas or wants_gastos or wants_productos):
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows,
                headers,
                source,
                uploaded_file_id,
                context_label="Tabla sin clasificar",
            )

    else:
        # ── Documentos de texto/imagen: inserción por línea (montos detectados) ──
        # F6-A4: los documentos de texto/imagen NO extraen fecha (ni nada
        # estructurado más allá de montos por línea vía AMOUNT_RE). Antes se
        # estampillaba "hoy" — fecha de negocio inventada (invariante 2d). Ahora la
        # línea va a /otros para que el usuario complete la fecha antes de importar.
        # Es degradado pero honesto: la lectura real de foto/PDF con fecha es F7.
        def _route_text_line_to_otros(entry: dict[str, Any], suggested: str) -> None:
            # Se rutea la línea UNA vez (no N veces por cada monto detectado). Solo si
            # trae al menos un monto válido — una línea sin monto no materializa un
            # pendiente vacío. `uploaded_file_id` liga el /otros al archivo origen.
            #
            # No se registra fingerprint por línea (a diferencia del path spreadsheet,
            # que ancla en (archivo, contexto, índice)): el path texto nunca tuvo
            # anclas de fila estables. La no-duplicación la garantiza F4 aguas arriba
            # — un confirm exitoso deja el archivo DONE y no puede re-confirmarse
            # (CAS del lease), y un fallo revierte el savepoint entero. La relectura
            # es un flujo distinto (reread_service) que reprocesa desde cero.
            if not any(_parse_amount(m) for m in entry.get("montos", [])):
                return
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows=[entry],
                headers=None,
                source=source,
                uploaded_file_id=uploaded_file_id,
                context_label=(
                    "Documento sin fecha reconocible: revisá el monto y completá la "
                    "fecha antes de importar"
                ),
                suggested_entity=suggested,
            )

        def _add_text_sale(entry: dict[str, Any]) -> None:
            _route_text_line_to_otros(entry, "sale")

        def _add_text_expense(entry: dict[str, Any]) -> None:
            _route_text_line_to_otros(entry, "expense")

        text_contexts = summary.get("mapping_contexts")
        if text_contexts:
            # Path por contexto: cada grupo detectado se incluye/excluye y puede
            # reasignarse a otro entity_type (context_entity).
            override = context_entity or {}
            text_bucket = {
                "sale": "ventas_detectadas",
                "expense": "gastos_detectados",
                "product": "stock_detectado",
            }
            for ctx in text_contexts:
                ctx_id = ctx.get("context_id")
                base_entity = ctx.get("entity_type")
                entity = override.get(ctx_id or "") or base_entity
                if entity not in ("sale", "expense"):
                    continue  # producto desde una línea de texto no es insertable
                # Inclusión: por contexto, o legacy por tipo según el entity efectivo.
                if context_confirmed:
                    if not context_confirmed.get(ctx_id):
                        continue
                elif not confirmed_fields.get(
                    "ventas" if entity == "sale" else "gastos"
                ):
                    continue
                rows = [
                    r
                    for r in summary.get(text_bucket.get(base_entity or "", ""), [])
                    if r.get("__context__") == ctx_id
                ]
                for text_row in rows:
                    if entity == "sale":
                        _add_text_sale(text_row)
                    else:
                        _add_text_expense(text_row)
        else:
            # Legacy: documentos viejos sin mapping_contexts.
            if confirmed_fields.get("ventas"):
                for text_row in summary.get("ventas_detectadas", []):
                    _add_text_sale(text_row)
            if confirmed_fields.get("gastos"):
                for text_row in summary.get("gastos_detectados", []):
                    _add_text_expense(text_row)

    await session.flush()
    # Persistir en lote (idempotente) las huellas nuevas del camino batch.
    if seen_fp is not None and _preloaded_fp is not None:
        await _persist_import_fingerprints(session, tenant_id, seen_fp - _preloaded_fp)
    if return_details:
        if stamp_product_updated_at:
            await _stamp_updated_at_on_product_details(session, product_details)
        counts["product_details"] = product_details
    return counts


_PAGO_COLS: set[str] = {"forma_pago", "metodo_pago", "payment_method", "medio_pago", "pago"}
# Tupla = prioridad: "categoria" debe ganar siempre sobre "tipo" (un CSV con
# columnas `categoria` Y `tipo` debe tomar la categoría de `categoria`).
_CATEGORIA_COLS: tuple[str, ...] = ("categoria", "category", "rubro", "tipo")
_RECURRENTE_COLS: set[str] = {"recurrente", "recurring", "es_fijo", "frecuencia"}

_TRUTHY_ES = {"si", "sí", "true", "1", "x", "verdadero", "fijo", "yes"}
_FALSY_ES = {"no", "false", "0", "variable", ""}


def _parse_bool_es(raw: Any) -> bool | None:
    """Parsea booleanos es-AR de archivos ('Sí'/'fijo'/'True'/'1'). None si es ambiguo."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUTHY_ES:
        return True
    if s in _FALSY_ES or s in {"none", "nan"}:
        return False
    return None
_CANTIDAD_COLS: set[str] = {"cantidad", "qty", "units", "unidades", "cant"}

# Monto de venta: preferimos "total" (precio_unitario × cantidad) sobre "precio_unitario"
_VENTA_TOTAL_COLS: set[str] = {"total", "importe_total", "total_venta", "monto_total"}


def _clean_str(val: Any, max_len: int = 99) -> str | None:
    """Convierte a string limpio o None si es nulo/nan."""
    if val is None:
        return None
    s = str(val).strip()
    return s[:max_len] if s and s.lower() not in {"none", "nan", ""} else None


async def _insert_multisheet_data(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None,
    today: datetime,
    return_details: bool,
    stamp_product_updated_at: bool = False,
    product_details: list[dict[str, Any]],
    counts: dict[str, Any],
    column_mappings: dict[str, str] | None,
    context_mappings: dict[str, dict[str, str]] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    context_entity: dict[str, str] | None = None,
    source: str = "ingestion",
    uploaded_file_id: uuid.UUID | None = None,
    seen_fp: set[str] | None = None,
    product_cache: dict[uuid.UUID, Any] | None = None,
    stock_is_purchase: bool = False,
) -> dict[str, Any]:
    """Importa datos de un archivo multi-contexto (multi-hoja) por contexto.

    Si el summary tiene `mapping_contexts`, itera por contexto: filtra filas por el
    marcador `__context__`, respeta la inclusión (`context_confirmed` o, en su
    defecto, `confirmed_fields` por tipo), y aplica el mapeo explícito de ese
    contexto (`context_mappings[context_id]`) con fallback a detección por keyword
    (`_row_val`). Si no hay `mapping_contexts` (summaries viejos), cae al path
    legacy por tipo con detección por keyword. Sin límite de filas.
    """
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    confirmed_fields = confirmed_fields or {}
    context_mappings = context_mappings or {}
    _flush_every = 500  # enviar a DB en batches para no acumular en memoria

    # FASE 3: índice de catálogo para el LINK de ventas/gastos/compras (en memoria).
    _by_sku, _by_name, _by_token = await _load_product_index(session, tenant_id)
    # Índice de proveedores para find-or-create en compras (una carga).
    _supplier_index = await _load_supplier_index(session, tenant_id)
    # FASE E: vertical del tenant para normalizar categorías de producto.
    _vertical = await _load_tenant_vertical(session, tenant_id)
    # Batch: balances en una query (evita un SELECT por fila en movimientos).
    _balance_index = await _load_balance_index(session, tenant_id)
    # F7c: índice de identidad de clientes para resolver la referencia por fila
    # en ventas — incluye los clientes recién creados por el paso maestro (arriba,
    # en _insert_confirmed_data_impl, antes de llegar acá).
    _customer_identity_index: dict[IdentityKey, Any] = await _load_customer_identity_index(
        session, tenant_id
    )
    _local_sentinel_id: uuid.UUID | None = None

    async def _get_local_sentinel() -> uuid.UUID:
        nonlocal _local_sentinel_id
        if _local_sentinel_id is None:
            from app.application.services.customer_sentinel import (  # noqa: PLC0415
                resolve_or_create_local_sentinel,
            )

            _local_sentinel_id = await resolve_or_create_local_sentinel(session, tenant_id)
        return _local_sentinel_id

    # F7c: modo de resolución de proveedor por fila en compras (ver docstring del
    # mismo bloque en _insert_confirmed_data_impl). "legacy" no carga el índice.
    _supplier_ref_mode = get_settings().SUPPLIER_REFERENCE_CREATION_MODE
    _supplier_identity_index: dict[IdentityKey, Any] = (
        await _load_supplier_identity_index(session, tenant_id)
        if _supplier_ref_mode == "link_only"
        else {}
    )
    # Traza agregada de decisiones de proveedor (Fase 1): reales desde compras,
    # marcas omitidas de catálogos, y uso del sentinela "No identificado".
    _real_suppliers: set[str] = set()
    _skipped_brands: set[str] = set()
    _sentinel_used = False
    # F2-T2: caché intra-corrida por CLAVE DE IDENTIDAD (sku o nombre+marca),
    # propia de esta función (no se comparte con _insert_confirmed_data_impl).
    # Evita duplicar producto cuando 2 filas del MISMO archivo comparten
    # identidad en un mismo bloque (<500) — con autoflush=False (prod) el
    # SELECT no ve el pendiente sin flush. Poblada al crear O al resolver
    # (status=resolved).
    products_by_identity_key: dict[str, Product] = {}
    # F2-T2: índices de identidad pre-cargados UNA vez para esta corrida (no un
    # SELECT por fila) — alimentan el motor de resolución en _add_product.
    _identity_indexes = await _load_product_identity_indexes(session, tenant_id)

    def _val(row: dict[str, Any], col: str | None, keywords: set[str] | tuple[str, ...]) -> Any:
        # Columna explícita (mapeo) si existe; si no, detección por keyword.
        if col:
            return row.get(col)
        return _row_val(row, keywords)

    def _custom_fields(row: dict[str, Any], cf_cols: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for cf_key, src in cf_cols.items():
            v = row.get(src)
            if v is not None and str(v).strip().lower() not in {"", "none", "nan"}:
                out[cf_key] = str(v)
        return out

    async def _add_sale(
        row: dict[str, Any],
        cols: dict[str, str],
        cf_cols: dict[str, str],
        row_ref: str | None = None,
    ) -> bool:
        """Inserta una venta. Devuelve ``True`` si insertó (monto parseable), ``False`` si no."""
        amount_col = cols.get("amount")
        amount = (
            _parse_amount(row.get(amount_col))
            if amount_col
            else _parse_amount(_row_val(row, _VENTA_TOTAL_COLS))
            or _parse_amount(_row_val(row, _VENTA_AMOUNT_COLS))
        )
        if not amount:
            return False
        raw_date = _val(row, cols.get("transaction_date") or cols.get("expense_date"), _FECHA_COLS)
        tx_date = _parse_date(raw_date) if raw_date is not None else None
        if tx_date is None:
            # F6-A2: sin fecha reconocible la venta va a /otros — no se inventa "hoy"
            # (invariante 2d). Devuelve True: la captura es output PERSISTIDO, así el
            # caller registra el fingerprint (re-subir no re-crea el UnclassifiedRecord).
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows=[row],
                headers=None,  # sin headers de hoja en este scope
                source=source,
                uploaded_file_id=uploaded_file_id,
                context_label="Fila sin fecha reconocible: no se puede registrar la venta",
                suggested_entity="sale",
            )
            return True
        qty: int = 1
        qty_raw = _val(row, cols.get("quantity"), _CANTIDAD_COLS)
        if qty_raw not in (None, "", "None", "nan"):
            try:
                qty = max(1, int(float(str(qty_raw))))
            except (ValueError, TypeError):
                qty = 1
        _name_col = cols.get("notes") or cols.get("product_name") or cols.get("name")
        notes = _clean_str(_val(row, _name_col, _NOMBRE_COLS), 499)
        # Canónico: antes se guardaba el texto crudo del archivo ("efectivo",
        # "mercadopago") y filtros/arqueo quedaban inconsistentes.
        pay_raw = _clean_str(_val(row, cols.get("payment_method"), _PAGO_COLS), 30)
        pay = normalize_payment_method(pay_raw) if pay_raw else "cash"
        entry = SaleEntry(
            tenant_id=tenant_id,
            amount=amount,
            quantity=qty,
            # Solo por mapeo explícito; nunca derivado de amount/quantity.
            unit_price=(
                _parse_amount(row.get(_up_col)) if (_up_col := cols.get("unit_price")) else None
            ),
            transaction_date=tx_date,
            payment_method=pay,
            notes=notes or "Importado desde archivo",
            provenance="REAL",
            source_upload_id=uploaded_file_id,
        )
        cf = _custom_fields(row, cf_cols)
        # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
        entry.product_id = _resolve_product(
            _by_sku,
            _by_name,
            _val(row, cols.get("product_name") or cols.get("name"), _NOMBRE_COLS),
            _val(row, cols.get("sku"), _SKU_COLS),
            _by_token,
            by_barcode=_identity_indexes.by_barcode,
            barcode=_val(row, cols.get("barcode"), _BARCODE_COLS),
        )
        # F7c: resolución de cliente por fila — matched/anonymous/unresolved.
        # Nunca crea: solo vincula (maestro importado arriba o ya en la DB) o cae
        # al sentinela "Local" con traza si la referencia no matchea.
        _cust_ref = _classify_row_reference(
            _customer_reference_record(row, cols),
            doc_fields=_CUSTOMER_DOC_FIELDS,
            existing_index=_customer_identity_index,
            anonymous_name_tokens=_ANONYMOUS_CUSTOMER_TOKENS,
        )
        if _cust_ref.outcome == "matched":
            assert _cust_ref.entity is not None
            entry.customer_id = _cust_ref.entity.id
            cf["_customer_resolution"] = "matched"
        else:
            entry.customer_id = await _get_local_sentinel()
            cf["_customer_resolution"] = _cust_ref.outcome
            if _cust_ref.outcome == "unresolved" and _cust_ref.raw_value:
                cf["_customer_reference_raw"] = _cust_ref.raw_value
        _bump_reference_counts(counts, "ventas_cliente", _cust_ref.outcome)
        if cf:
            entry.custom_fields = cf
        if row_ref is not None:
            entry.source_row_ref = row_ref  # Mejora D
        session.add(entry)
        counts["ventas"] += 1
        return True

    async def _add_expense(
        row: dict[str, Any],
        cols: dict[str, str],
        cf_cols: dict[str, str],
        row_ref: str | None = None,
    ) -> bool:
        """Inserta un gasto. Devuelve ``True`` si insertó (monto parseable), ``False`` si no."""
        amount_col = cols.get("amount")
        amount = (
            _parse_amount(row.get(amount_col))
            if amount_col
            else _parse_amount(_row_val(row, _VENTA_TOTAL_COLS))  # "total" también en compras
            or _parse_amount(_row_val(row, _GASTO_AMOUNT_COLS))
        )
        if not amount:
            return False
        raw_date = _val(row, cols.get("expense_date") or cols.get("transaction_date"), _FECHA_COLS)
        tx_date = _parse_date(raw_date) if raw_date is not None else None
        if tx_date is None:
            # F6-A2: sin fecha reconocible el gasto va a /otros — no se inventa "hoy"
            # (invariante 2d). Devuelve True para que el caller registre el fingerprint.
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows=[row],
                headers=None,
                source=source,
                uploaded_file_id=uploaded_file_id,
                context_label="Fila sin fecha reconocible: no se puede registrar el gasto",
                suggested_entity="expense",
            )
            return True
        _name_col = cols.get("notes") or cols.get("product_name") or cols.get("name")
        desc = _clean_str(_val(row, _name_col, _NOMBRE_COLS), 499)
        pay_raw = _clean_str(_val(row, cols.get("payment_method"), _PAGO_COLS), 30)
        pay = normalize_payment_method(pay_raw) if pay_raw else "transfer"
        # _row_val_categoria saltea columnas de pago (tipo_pago ≠ categoría).
        cat_raw = _clean_str(
            row.get(cols["category"]) if cols.get("category") else _row_val_categoria(row),
            99,
        )
        # Categoría de producto del vertical ("Bebidas") = compra de mercadería
        # → INVENTORY/COGS, preservando el texto original como label.
        cat_code, cat_label, _ = classify_expense_with_vertical(cat_raw, _vertical)
        recurring = _parse_bool_es(_val(row, cols.get("is_recurring"), _RECURRENTE_COLS))
        expense = ExpenseEntry(
            tenant_id=tenant_id,
            amount=amount,
            category=cat_code,
            transaction_date=tx_date,
            description=desc or "Gasto importado",
            is_recurring=recurring if recurring is not None else False,
            payment_method=pay,
            provenance="REAL",
            source_upload_id=uploaded_file_id,
        )
        cf = _custom_fields(row, cf_cols)
        if cat_label:
            cf = {**cf, "category_label": cat_label}
        # Capturar proveedor real si la fila/mapeo lo trae. F7c: gobernado por
        # SUPPLIER_REFERENCE_CREATION_MODE — ver el mismo bloque en
        # _insert_confirmed_data_impl (single-context) para el detalle completo.
        # "legacy" (default): sin cambios, find-or-create por nombre. "link_only":
        # matchea por identidad y NUNCA crea; el anonymous/unresolved sin match se
        # difiere al bloque de abajo, que ya sabe si es compra de mercadería.
        _pending_supplier_ref: RowReferenceResolution | None = None
        # Review 7d (Important): diferido igual que en el path single-context —
        # ver el comentario en _insert_confirmed_data_impl.
        _supplier_matched = False
        _supplier_name_raw = _val(row, cols.get("supplier_name"), _PROVEEDOR_COLS)
        if _supplier_ref_mode == "link_only":
            _has_supplier_ref_col = bool(
                _supplier_name_raw is not None
                or cols.get("supplier_cuil")
                or cols.get("supplier_email")
                or cols.get("supplier_phone")
            )
            if _has_supplier_ref_col:
                _sup_ref = _classify_row_reference(
                    _supplier_reference_record(row, cols, _supplier_name_raw),
                    doc_fields=_SUPPLIER_DOC_FIELDS,
                    existing_index=_supplier_identity_index,
                )
                if _sup_ref.outcome == "matched":
                    assert _sup_ref.entity is not None
                    expense.supplier_id = _sup_ref.entity.id
                    expense.supplier_name = _sup_ref.entity.name
                    cf["_supplier_resolution"] = "matched"
                    _supplier_matched = True
                else:
                    _pending_supplier_ref = _sup_ref
        elif _supplier_name_raw is not None:
            (
                expense.supplier_id,
                expense.supplier_name,
            ) = await _resolve_or_create_supplier(
                session, tenant_id, _supplier_name_raw, _supplier_index
            )
            if expense.supplier_name:
                _real_suppliers.add(expense.supplier_name)
        # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
        _exp_name = _val(row, cols.get("product_name") or cols.get("name"), _NOMBRE_COLS)
        _exp_sku = _val(row, cols.get("sku"), _SKU_COLS)
        _exp_barcode = _val(row, cols.get("barcode"), _BARCODE_COLS)
        expense.product_id = _resolve_product(
            _by_sku,
            _by_name,
            _exp_name,
            _exp_sku,
            _by_token,
            by_barcode=_identity_indexes.by_barcode,
            barcode=_exp_barcode,
        )
        # Costo unitario: columna inequívoca y DISTINTA de la del monto ("costo"
        # a secas suele ser el total de la línea — no es unit_cost).
        _amount_src = (
            amount_col
            or _row_col(row, _VENTA_TOTAL_COLS)
            or _row_col(row, _GASTO_AMOUNT_COLS)
        )
        _uc_col = cols.get("unit_cost_ars") or _row_col(row, _COSTO_UNITARIO_COLS)
        # Mejora C: nunca tomar una columna de "costo total" como costo unitario.
        unit_cost = (
            _parse_amount(row.get(_uc_col))
            if _uc_col and _uc_col != _amount_src and not _is_total_cost_col(_uc_col)
            else None
        )
        exp_qty_raw = _val(row, cols.get("quantity"), _CANTIDAD_COLS)
        # Compra de mercadería = gasto COGS+caja Y alta/reposición de producto. Señal
        # de fila: nombre de producto + cantidad>0 (libro de compras). Se CREA el
        # producto ANTES de inferir expense_type para que un producto NUEVO (no en
        # catálogo, sin categoría) quede COGS por su product_id — el orden inverso lo
        # dejaba OPEX y nunca se creaba. Servicios/alquiler (sin cantidad) NO crean.
        _has_qty = _parse_qty(exp_qty_raw) > 0
        _is_merch_purchase = bool(_clean_str(_exp_name, 299)) and _has_qty
        if expense.product_id is None and (
            _is_merch_purchase or (cat_code == "INVENTORY" and _has_qty)
        ):
            _action, _pid, _cands = await _resolve_purchase_identity(
                session,
                tenant_id,
                name=_exp_name,
                sku=_exp_sku,
                brand=None,
                barcode=_exp_barcode,
                unit_cost=unit_cost,
                indexes=_identity_indexes,
                cache=products_by_identity_key,
                product_cache=product_cache,
            )
            if _action == "otros":
                # Review F2 #1: compra ambigua/en conflicto NO crea un 3er producto
                # duplicado — la fila va a "Otros" y el gasto NO se registra.
                counts["otros"] += _capture_unclassified(
                    session,
                    tenant_id,
                    rows=[row],
                    headers=None,  # sin headers de hoja en este scope
                    source=source,
                    uploaded_file_id=uploaded_file_id,
                    context_label="Compra de producto ambiguo: coincide con "
                    "varios productos del catálogo",
                    suggested_entity="expense",
                    match_candidates=_cands,
                )
                # Review F2 #6: la captura a Otros es output PERSISTIDO → devolver
                # True para que el caller registre el fingerprint (re-subir el
                # archivo no debe re-crear el UnclassifiedRecord).
                return True
            expense.product_id = _pid
            # Review F2 #3: solo "created" creó un producto incompleto.
            if _action == "created":
                counts["sin_producto"] += 1
            # Review F2 #2: registrar en los índices transaccionales para que
            # ventas/gastos POSTERIORES del mismo archivo puedan vincularlo.
            if _pid is not None:
                _register_product_transaction_indexes(
                    _pid, _exp_name, _exp_sku, _by_sku, _by_name, _by_token
                )
        # FASE D: discriminador COGS/OPEX (producto del catálogo/recién creado o
        # categoría INVENTORY) + stock desde compras con cantidad.
        expense.expense_type = infer_expense_type(cat_code, product_id=expense.product_id)
        # Sentinela: compra de mercadería (con product_id) SIN proveedor informado
        # → "No identificado" (UNO por tenant). No aplica a OPEX sin producto.
        if expense.supplier_id is None and expense.product_id is not None:
            nonlocal _sentinel_used
            expense.supplier_id = await _resolve_or_create_sentinel_supplier(
                session, tenant_id, _supplier_index
            )
            _sentinel_used = True
            counts["sin_proveedor"] += 1
            # F7c (link_only): recién acá se sabe que es compra de mercadería —
            # aplicar la clasificación diferida (anonymous/unresolved). En OPEX
            # (product_id None) el sentinela nunca se toca, así que una
            # referencia sin matchear en un gasto operativo queda sin proveedor
            # y sin traza, igual que hoy.
            if _supplier_ref_mode == "link_only":
                _outcome = (
                    _pending_supplier_ref.outcome
                    if _pending_supplier_ref is not None
                    else "anonymous"
                )
                cf["_supplier_resolution"] = _outcome
                _raw_pending = _pending_supplier_ref.raw_value if _pending_supplier_ref else None
                if _outcome == "unresolved" and _raw_pending:
                    cf["_supplier_reference_raw"] = _raw_pending
                _bump_reference_counts(counts, "compras_proveedor", _outcome)
        elif _supplier_matched:
            # Review 7d (Important): recién acá se sabe que la fila no fue
            # descartada a "Otros" (ese branch ya retornó antes) — bumpear en
            # el momento del match contaba una compra que podía no persistirse.
            _bump_reference_counts(counts, "compras_proveedor", "matched")
        if cf:
            expense.custom_fields = cf
        await _apply_purchase_to_stock(
            session,
            tenant_id,
            expense,
            exp_qty_raw,
            unit_cost,
            balance_index=_balance_index,
            product_cache=product_cache,
            source_row_ref=row_ref,
        )
        if row_ref is not None:
            expense.source_row_ref = row_ref  # Mejora D
        session.add(expense)
        counts["gastos"] += 1
        return True

    async def _add_product(
        row: dict[str, Any],
        cols: dict[str, str],
        cf_cols: dict[str, str],
        row_ref: str | None = None,
    ) -> bool:
        """Devuelve ``True`` si la fila se CAPTURÓ a /otros (identidad ambigua o fecha
        de producto ilegible en columna mapeada a mano) — el caller registra la huella
        de esa captura. ``False`` si creó/actualizó el producto o si no había nombre
        (la creación normal dedupea por identidad, no por huella)."""
        _name_col = cols.get("name") or cols.get("product_name")
        name = _clean_str(_val(row, _name_col, _NOMBRE_COLS), 299)
        if not name:
            return False
        # La columna "Tienda"/"proveedor" de un CATÁLOGO es marca/origen del
        # artículo, NO un proveedor: se guarda como atributo del producto en
        # ``custom_fields["marca"]``. NO se crea Supplier desde un catálogo.
        store_name: str | None = _clean_str(
            _val(row, cols.get("supplier_name"), _PROVEEDOR_COLS), 300
        )
        if store_name:
            _skipped_brands.add(store_name)
        # Mejora C: costo unitario narrow-first. Se resuelve ANTES que el precio
        # para poder excluirlo (desambiguar "precio de compra" vs "precio de
        # venta"). Mapeo explícito gana; si no, una columna inequívoca de costo
        # unitario (incluye "compra"); la broad ("costo") solo si no es la del
        # precio ni una de "costo total".
        _uc_mapped = cols.get("unit_cost_ars")
        _uc_col: str | None = None
        if _uc_mapped:
            cost = _parse_amount(row.get(_uc_mapped))
            _uc_col = _uc_mapped
        else:
            _uc_col = _row_col(row, _COSTO_UNITARIO_PRODUCT_COLS)
            if not _uc_col:
                _broad = _row_col(row, _COSTO_COLS)
                if _broad and not _is_total_cost_col(_broad):
                    _uc_col = _broad
            cost = (
                _parse_amount(row.get(_uc_col))
                if _uc_col and not _is_total_cost_col(_uc_col)
                else None
            )
        # Precio de venta desambiguado del de compra/costo: mapeo explícito gana;
        # si no, "venta" > "lista" > "precio_venta"/"p_venta" > genérico "precio"
        # EXCLUYENDO la columna de costo ya resuelta y cualquier header de
        # compra/costo. Antes el genérico tomaba "Precio de compra" (caso ASTERIA).
        _price_mapped = cols.get("sale_price_ars")
        if _price_mapped:
            price = _parse_amount(row.get(_price_mapped))
        else:
            _price_col = _resolve_sale_price_col(list(row.keys()), _uc_col)
            price = _parse_amount(row.get(_price_col)) if _price_col else None
        # Precio de lista (sugerido): SOLO por mapeo explícito, sin fallback
        # heurístico — si nadie declaró qué columna es el sugerido, el dato no
        # existe y queda NULL en vez de adivinarse desde un header parecido.
        _list_mapped = cols.get("list_price_ars")
        list_price = _parse_amount(row.get(_list_mapped)) if _list_mapped else None
        try:
            stock_raw = _val(row, cols.get("stock_units"), _STOCK_COLS)
            stock_val = (
                int(float(str(stock_raw)))
                if stock_raw not in (None, "", "None", "nan")
                else 0
            )
        except (ValueError, TypeError):
            stock_val = 0
        sku = _clean_str(_val(row, cols.get("sku"), _SKU_COLS), 99)
        # F2-T5: código de barras (columna mapeada o detección por keyword).
        barcode = _clean_str(_val(row, cols.get("barcode"), _BARCODE_COLS), 64)
        # FASE E: categoría canónica del vertical; sin columna → None.
        cat_raw = _clean_str(
            row.get(cols["category"]) if cols.get("category") else _row_val_categoria(row),
            99,
        )
        cat: str | None = None
        cat_label: str | None = None
        if cat_raw:
            cat, cat_label = normalize_product_category(cat_raw, _vertical)
        # F6-B2: fechas de producto (columna mapeada o keyword; la genérica "fecha"
        # NO cuenta). Sin columna/celda vacía/heurística ilegible → None (un producto
        # es válido sin fecha, no se inventa). PERO si un campo MAPEADO A MANO trae un
        # valor no vacío ilegible → la fila va a /otros SIN aplicarse (no se toca
        # stock, precio ni identidad). acquired_at naive; expiry_date date.
        _acq_raw = _val(row, cols.get("acquired_at"), _ACQUIRED_COLS)
        _acquired = parse_business_datetime(_acq_raw) if _acq_raw is not None else None
        if _acquired is not None and _acquired.tzinfo is not None:
            _acquired = _acquired.replace(tzinfo=None)
        _exp_raw = _val(row, cols.get("expiry_date"), _EXPIRY_COLS)
        _expiry = parse_business_date(_exp_raw) if _exp_raw is not None else None
        if _product_date_invalid_explicit(
            _acq_raw, _acquired, cols.get("acquired_at") is not None
        ) or _product_date_invalid_explicit(
            _exp_raw, _expiry, cols.get("expiry_date") is not None
        ):
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows=[row],
                headers=None,
                source=source,
                uploaded_file_id=uploaded_file_id,
                context_label=(
                    "Fecha de producto ilegible en una columna que mapeaste a mano: "
                    "revisá y completá antes de importar"
                ),
                suggested_entity="product",
            )
            return True
        # F2-T2: resolución de identidad por claves independientes
        # (barcode→sku→nombre+marca). Caché intra-corrida ANTES del motor —
        # evita duplicar con autoflush=False cuando 2 filas del archivo
        # comparten identidad (mismo patrón que F1).
        _sku_n = normalize_sku(sku)
        _name_n = normalize_product_name(name)
        _brand_n = normalize_brand(store_name)
        _bc_n = normalize_barcode(barcode)
        async def _merge_into_existing(existing: Product) -> None:
            """Aplica la fila del catálogo a un producto que YA existe.

            Extraído en F5-A porque ahora hay dos caminos que llegan acá: el motor de
            identidad resolvió el producto, o el índice único rechazó la creación y
            ``add_product_or_reuse`` devolvió al ocupante. Duplicar este cuerpo era el
            riesgo real: en el camino de creación ``_apply_catalog_stock`` recibe
            ``delta=stock_val`` (desde 0), y aplicárselo a un producto preexistente le
            SUMARÍA su stock actual otra vez.
            """
            before_snap: dict[str, Any] | None = None
            if return_details:
                # F9b (Task 6): ver el comentario análogo en
                # ``_insert_confirmed_data_impl`` — mismo criterio, gap propio de
                # esta función (antes NUNCA poblaba ``product_details``, ni
                # siquiera con precio/stock; camino "mixed"/multi-hoja, F8+).
                before_snap = {
                    "sale_price_ars": str(existing.sale_price_ars),
                    # Revisión final F9b (Hallazgo 1): ver el comentario análogo en
                    # ``_insert_confirmed_data_impl`` — el mecanismo incremental de
                    # movimientos NUNCA cubre `unit_cost_ars` (solo stock_units), así
                    # que necesita snapshot propio para que el undo lo pueda restaurar.
                    "unit_cost_ars": (
                        str(existing.unit_cost_ars)
                        if existing.unit_cost_ars is not None
                        else None
                    ),
                    # Mismo caso que unit_cost_ars: sin snapshot propio el undo no
                    # lo puede restaurar.
                    "list_price_ars": (
                        str(existing.list_price_ars)
                        if existing.list_price_ars is not None
                        else None
                    ),
                    "stock_units": existing.stock_units,
                    "sku": existing.sku,
                    "barcode": existing.barcode,
                    "category": existing.category,
                    "acquired_at": (
                        existing.acquired_at.isoformat() if existing.acquired_at else None
                    ),
                    "expiry_date": (
                        existing.expiry_date.isoformat() if existing.expiry_date else None
                    ),
                }
            if price:
                existing.sale_price_ars = price
            if cost:
                existing.unit_cost_ars = cost
            if list_price:
                existing.list_price_ars = list_price
            if stock_val > 0:
                _delta = stock_val - existing.stock_units
                existing.stock_units = stock_val
                # A2/A5: movimiento estampado catalog_initial_stock + COGS del delta.
                await _apply_catalog_stock(
                    session,
                    tenant_id,
                    product_id=existing.id,
                    product_name=name,
                    delta=_delta,
                    final_qty=stock_val,
                    unit_cost=cost,
                    store_name=store_name,
                    tx_date=today,
                    uploaded_file_id=uploaded_file_id,
                    source_row_ref=row_ref,
                    balance_index=_balance_index,
                    is_purchase=stock_is_purchase,
                )
            if sku:
                existing.sku = sku
            if barcode and not existing.barcode:  # F2-T5: completar sin pisar
                existing.barcode = barcode
            if cat and not existing.category:
                existing.category = cat
            # F6-B2: acumular fechas de producto salvo edición manual del usuario
            # (has_user_edits protege ambas). acquired_at = la más antigua conocida;
            # expiry_date por la regla futuro-más-próximo / vencido-más-reciente.
            if not existing.has_user_edits:
                _acc_acq = _accumulate_acquired_at(existing.acquired_at, _acquired)
                if _acc_acq is not None:
                    existing.acquired_at = _acc_acq
                _acc_exp = _accumulate_expiry_date(
                    existing.expiry_date, _expiry, today.date()
                )
                if _acc_exp is not None:
                    existing.expiry_date = _acc_exp
            _register_product_identity_cache(
                products_by_identity_key, existing, _sku_n, _name_n, _brand_n, _bc_n
            )
            if return_details:
                product_details.append(
                    {
                        "action": "UPDATED",
                        "product_id": str(existing.id),
                        "name": name,
                        "before": before_snap,
                        "after": {
                            "sale_price_ars": str(price or existing.sale_price_ars),
                            "list_price_ars": (
                                str(list_price or existing.list_price_ars)
                                if (list_price or existing.list_price_ars) is not None
                                else None
                            ),
                            "unit_cost_ars": (
                                str(cost or existing.unit_cost_ars)
                                if (cost or existing.unit_cost_ars) is not None
                                else None
                            ),
                            "stock_units": stock_val or existing.stock_units,
                            "sku": existing.sku,
                            "barcode": existing.barcode,
                            "category": existing.category,
                            "acquired_at": (
                                existing.acquired_at.isoformat()
                                if existing.acquired_at
                                else None
                            ),
                            "expiry_date": (
                                existing.expiry_date.isoformat()
                                if existing.expiry_date
                                else None
                            ),
                        },
                    }
                )

        def _route_ambiguous_to_otros(
            candidates: list[dict[str, Any]], context_label: str
        ) -> None:
            """La fila ambigua NO se descarta en silencio: queda en /otros con los
            candidatos, para revisión/unificación manual."""
            counts["productos_ambiguos"] += 1
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows=[row],
                headers=None,  # sin headers de hoja disponibles en este scope
                source=source,
                uploaded_file_id=uploaded_file_id,
                context_label=context_label,
                suggested_entity="product",
                match_candidates=candidates,
            )

        existing = _lookup_product_identity_cache(
            products_by_identity_key, _sku_n, _name_n, _brand_n, _bc_n
        )
        if existing is None:
            _resolution = _resolve_product_identity(
                name, sku, store_name, indexes=_identity_indexes, barcode=barcode
            )
            if _resolution.status in ("ambiguous", "conflict"):
                logger.warning(
                    "ingestion.product_name_ambiguous",
                    tenant_id=str(tenant_id),
                    normalized_name=_name_n,
                    row_ref=row_ref,
                    uploaded_file_id=str(uploaded_file_id) if uploaded_file_id else None,
                    count=len(_resolution.candidates),
                    candidate_ids=[c["id"] for c in _resolution.candidates],
                    match_strategy=_resolution.status,
                )
                _route_ambiguous_to_otros(
                    _resolution.candidates,
                    f"Producto ambiguo: coincide con {len(_resolution.candidates)} "
                    "productos activos"
                    if _resolution.status == "ambiguous"
                    else "Conflicto de identidad: el SKU y el nombre "
                    "apuntan a productos distintos",
                )
                return True  # capturado a /otros: no se importa, no se toca nada
            if _resolution.status == "resolved" and _resolution.product_id is not None:
                existing = await session.get(Product, _resolution.product_id)
        if existing:
            await _merge_into_existing(existing)
        else:
            cf = _custom_fields(row, cf_cols)
            if cat_label:
                cf = {**cf, "category_label": cat_label}
            if store_name:
                cf = {**cf, "marca": store_name}
            _new_id = uuid.uuid4()
            new_product = Product(
                id=_new_id,
                tenant_id=tenant_id,
                name=name,
                sku=sku,
                barcode=barcode,  # F2-T5
                # FASE 3 (B2): precio default 0 explícito para auto-creados incompletos.
                sale_price_ars=price or Decimal("0"),
                list_price_ars=list_price,
                unit_cost_ars=cost,
                stock_units=stock_val,
                category=cat,
                low_stock_threshold_units=None,
                provenance="REAL",
                # FASE 3 (B2): falta precio o costo → el usuario debe completarlo.
                requires_completion=not price or not cost,
                # F6-B2: fechas de producto del archivo (None si no se mapearon).
                acquired_at=_acquired,
                expiry_date=_expiry,
                # `{}` y no `None`: un `None` explícito persiste como JSON `null`
                # ('null'::jsonb) y rompe `GET /products` con 503 (ver arriba).
                custom_fields=cf or {},
                source_row_ref=row_ref,  # Mejora D
            )
            # F5-A: sin ``session.add`` — ``add_product_or_reuse`` necesita el objeto
            # TRANSIENT para emitir el INSERT dentro del savepoint.
            try:
                _resolved, _created = await add_product_or_reuse(session, new_product)
            except ProductIdentityConflictError as _conflict:
                # Ambigüedad que el motor no vio (barcode y sku de productos
                # distintos): mismo destino que la ambigüedad detectada arriba.
                logger.warning(
                    "ingestion.product_identity_ambiguous_on_insert",
                    tenant_id=str(tenant_id),
                    normalized_name=_name_n,
                    row_ref=row_ref,
                    matched_by=_conflict.matched_by,
                    candidate_ids=[str(p.id) for p in _conflict.candidates],
                )
                _route_ambiguous_to_otros(
                    _candidates_from_conflict(_conflict),
                    "Conflicto de identidad: el código de barras y el SKU "
                    "apuntan a productos distintos",
                )
                return True  # capturado a /otros
            if not _created:
                # El índice único resolvió una carrera: es exactamente el camino de
                # "producto existente" —incluido el delta de stock relativo—.
                await _merge_into_existing(_resolved)
                counts["productos"] += 1
                return False
            _register_product_identity_cache(
                products_by_identity_key, new_product, _sku_n, _name_n, _brand_n, _bc_n
            )
            # A2/A5: movimiento estampado catalog_initial_stock + COGS (stock inicial
            # = compra real, si trae costo).
            await _apply_catalog_stock(
                session,
                tenant_id,
                product_id=_new_id,
                product_name=name,
                delta=stock_val,
                final_qty=stock_val,
                unit_cost=cost,
                store_name=store_name,
                tx_date=today,
                uploaded_file_id=uploaded_file_id,
                source_row_ref=row_ref,
                balance_index=_balance_index,
                is_purchase=stock_is_purchase,
            )
            if return_details:
                product_details.append(
                    {
                        "action": "CREATED",
                        "product_id": str(_new_id),
                        "name": name,
                        "before": None,
                        "after": {
                            "sale_price_ars": str(price or Decimal("0")),
                            "list_price_ars": (
                                str(new_product.list_price_ars)
                                if new_product.list_price_ars is not None
                                else None
                            ),
                            "unit_cost_ars": (
                                str(new_product.unit_cost_ars)
                                if new_product.unit_cost_ars is not None
                                else None
                            ),
                            "stock_units": stock_val,
                            "sku": new_product.sku,
                            "barcode": new_product.barcode,
                            "category": new_product.category,
                            "acquired_at": (
                                new_product.acquired_at.isoformat()
                                if new_product.acquired_at
                                else None
                            ),
                            "expiry_date": (
                                new_product.expiry_date.isoformat()
                                if new_product.expiry_date
                                else None
                            ),
                        },
                    }
                )
        counts["productos"] += 1
        return False  # creó/actualizó producto: no es captura

    entity_bucket = {
        "sale": "ventas_detectadas",
        "expense": "gastos_detectados",
        "product": "stock_detectado",
    }
    entity_confirm_key = {"sale": "ventas", "expense": "gastos", "product": "productos"}

    contexts = summary.get("mapping_contexts")
    if contexts:
        # ── Path por contexto (hoja/grupo) ──────────────────────────────────────
        for ctx in contexts:
            ctx_id = ctx.get("context_id")
            base_entity = ctx.get("entity_type")
            # FASE F: el usuario puede reasignar una hoja no clasificada a un
            # tipo importable (context_entity), igual que en documentos de texto.
            entity = (context_entity or {}).get(ctx_id or "") or base_entity
            if entity in ("customer", "supplier"):
                # F7c: maestros ya importados en _import_master_entities, ANTES de
                # este dispatch (orden maestro→transacción) — nada más que hacer.
                continue
            if entity not in entity_bucket:
                # Hoja no clasificada y no reasignada → bandeja "Otros".
                otros_rows = [
                    r
                    for r in summary.get("otros_detectados", [])
                    if r.get("__context__") == ctx_id
                ]
                if otros_rows:
                    counts["otros"] += _capture_unclassified(
                        session,
                        tenant_id,
                        otros_rows,
                        ctx.get("headers"),
                        source,
                        uploaded_file_id,
                        context_label=str(ctx.get("label") or ctx_id or ""),
                    )
                continue
            # Inclusión: por contexto si vino context_confirmed; si no, por tipo (legacy)
            if context_confirmed:
                if not context_confirmed.get(ctx_id):
                    continue
            elif not confirmed_fields.get(entity_confirm_key[entity]):
                continue
            # Las filas viven en el bucket del tipo ORIGINAL de la hoja (o en
            # otros_detectados si era no clasificada y fue reasignada).
            bucket_key = entity_bucket.get(base_entity or "", "otros_detectados")
            bucket = summary.get(bucket_key, [])
            rows = [r for r in bucket if r.get("__context__") == ctx_id]
            if not rows:
                continue
            mapping = context_mappings.get(ctx_id or "", {})
            cols, cf_cols = _resolve_target_cols(mapping) if mapping else ({}, {})
            for _i, row in enumerate(rows):
                # B1: idempotencia por (archivo, contexto, índice). Chequeo
                # READ-ONLY; la huella se registra recién DESPUÉS y solo si la
                # fila insertó (una fila sin monto no queda quemada). Productos no
                # se dedupean acá (su identidad es nombre/SKU, ya manejada por upsert).
                _ctx_anchor = (
                    _import_row_anchor(tenant_id, uploaded_file_id, ctx_id, _i)
                    if entity in ("sale", "expense") and uploaded_file_id is not None
                    else None
                )
                if _ctx_anchor is not None and await _import_row_seen(
                    session, tenant_id, _ctx_anchor, seen_fp
                ):
                    continue
                # Mejora D: ref de fila origen (incluye productos, que no
                # fingerprintean pero sí trazan su fila). Usa el mismo ancla por
                # (archivo, contexto, índice).
                _row_ref = (
                    _source_row_ref(
                        _import_row_anchor(tenant_id, uploaded_file_id, ctx_id, _i)
                    )
                    if uploaded_file_id is not None
                    else None
                )
                if entity == "sale":
                    _did_insert = await _add_sale(row, cols, cf_cols, _row_ref)
                elif entity == "expense":
                    _did_insert = await _add_expense(row, cols, cf_cols, _row_ref)
                else:
                    # F6-B2: la CREACIÓN de producto no fingerprintea (dedup por
                    # identidad/upsert), pero SÍ su CAPTURA a /otros (identidad
                    # ambigua o fecha ilegible mapeada a mano) — ancla propia
                    # "producto:{ctx}" para no re-capturar en una relectura.
                    _prod_cap_anchor = (
                        _import_row_anchor(
                            tenant_id, uploaded_file_id, f"producto:{ctx_id}", _i
                        )
                        if uploaded_file_id is not None
                        else None
                    )
                    if _prod_cap_anchor is not None and await _import_row_seen(
                        session, tenant_id, _prod_cap_anchor, seen_fp
                    ):
                        continue
                    _prod_captured = await _add_product(row, cols, cf_cols, _row_ref)
                    if _prod_captured and _prod_cap_anchor is not None:
                        await _register_import_row_fingerprint(
                            session, tenant_id, _prod_cap_anchor, seen_fp
                        )
                    _did_insert = False  # su huella (si hubo captura) ya se registró
                if _ctx_anchor is not None and _did_insert:
                    await _register_import_row_fingerprint(
                        session, tenant_id, _ctx_anchor, seen_fp
                    )
                if (_i + 1) % _flush_every == 0:
                    await session.flush()
    else:
        # ── Legacy: summaries sin mapping_contexts. Detección por keyword por tipo. ──
        if confirmed_fields.get("ventas"):
            for _i, row in enumerate(summary.get("ventas_detectadas", [])):
                # Chequeo READ-ONLY; registrar recién después y solo si insertó.
                _v_anchor = (
                    _import_row_anchor(tenant_id, uploaded_file_id, "ventas", _i)
                    if uploaded_file_id is not None
                    else None
                )
                if _v_anchor is not None and await _import_row_seen(
                    session, tenant_id, _v_anchor, seen_fp
                ):
                    continue
                if (
                    await _add_sale(row, {}, {}, _source_row_ref(_v_anchor))
                    and _v_anchor is not None
                ):
                    await _register_import_row_fingerprint(
                        session, tenant_id, _v_anchor, seen_fp
                    )
                if (_i + 1) % _flush_every == 0:
                    await session.flush()
        if confirmed_fields.get("gastos"):
            for _j, row in enumerate(summary.get("gastos_detectados", [])):
                _g_anchor = (
                    _import_row_anchor(tenant_id, uploaded_file_id, "gastos", _j)
                    if uploaded_file_id is not None
                    else None
                )
                if _g_anchor is not None and await _import_row_seen(
                    session, tenant_id, _g_anchor, seen_fp
                ):
                    continue
                if (
                    await _add_expense(row, {}, {}, _source_row_ref(_g_anchor))
                    and _g_anchor is not None
                ):
                    await _register_import_row_fingerprint(
                        session, tenant_id, _g_anchor, seen_fp
                    )
        if confirmed_fields.get("productos"):
            for _k, row in enumerate(summary.get("stock_detectado", [])):
                _p_ref = (
                    _source_row_ref(
                        _import_row_anchor(tenant_id, uploaded_file_id, "productos", _k)
                    )
                    if uploaded_file_id is not None
                    else None
                )
                # F6-B (review): idempotencia de la captura a /otros (identidad
                # ambigua) también en el path legacy — ancla propia para no re-capturar
                # en una relectura, igual que en single/multi-context. Namespace
                # "producto:productos" disjunto del ancla de row_ref (contexto
                # "productos"). Los productos normales (return False) no fingerprintean.
                _prod_cap_anchor = (
                    _import_row_anchor(
                        tenant_id, uploaded_file_id, "producto:productos", _k
                    )
                    if uploaded_file_id is not None
                    else None
                )
                if _prod_cap_anchor is not None and await _import_row_seen(
                    session, tenant_id, _prod_cap_anchor, seen_fp
                ):
                    continue
                if (
                    await _add_product(row, {}, {}, _p_ref)
                    and _prod_cap_anchor is not None
                ):
                    await _register_import_row_fingerprint(
                        session, tenant_id, _prod_cap_anchor, seen_fp
                    )

    # Traza agregada (Fase 1) de las decisiones de proveedor del multi-hoja.
    if _real_suppliers:
        _audit_supplier_decision(
            session,
            tenant_id,
            decision_type="SUPPLIER_CREATED_FROM_PURCHASE",
            data={"suppliers": sorted(_real_suppliers), "count": len(_real_suppliers)},
        )
    if _sentinel_used:
        _audit_supplier_decision(
            session,
            tenant_id,
            decision_type="SUPPLIER_SENTINEL_CREATED",
            data={"name": _SENTINEL_SUPPLIER_NAME},
        )
    if _skipped_brands:
        _audit_supplier_decision(
            session,
            tenant_id,
            decision_type="SUPPLIER_SKIPPED_FROM_CATALOG",
            data={"skipped_brands": sorted(_skipped_brands), "count": len(_skipped_brands)},
        )

    await session.flush()
    if return_details:
        if stamp_product_updated_at:
            await _stamp_updated_at_on_product_details(session, product_details)
        counts["product_details"] = product_details
    return counts


async def bulk_import_unclassified(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_filter: str | None = None,
) -> dict[str, int]:
    """Importa en lote los ``unclassified_records`` PENDING sugeridos como
    venta/gasto cuya fila cruda parsea (fecha + monto).

    Reusa la misma resolución por keyword y normalizaciones canónicas que el
    import de archivos: categoría con vertical (mercadería → INVENTORY/COGS),
    payment_method canónico y vínculo al producto del catálogo. Lo que no
    parsea queda PENDING (el modal por registro permite completarlo a mano);
    los sugeridos como producto no entran acá (necesitan campos que la fila
    suelta no garantiza). Devuelve ``{imported_sales, imported_expenses,
    skipped, needs_manual}`` — ``skipped`` = ya importados (idempotencia);
    ``needs_manual`` = no parsearon (fecha/monto ilegible) y requieren que el
    usuario los complete en el modal por registro. Antes se mezclaban en un solo
    contador y la UI no podía decir cuántos exigían atención (F6-A5).
    """
    from datetime import UTC  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415
    from app.persistence.models.unclassified_record import (  # noqa: PLC0415
        UNCLASSIFIED_STATUS_IMPORTED,
        UNCLASSIFIED_STATUS_PENDING,
        UnclassifiedRecord,
    )

    # F3-T3: importación en lote crea productos/stock por fuera de
    # ProductRepository.save. Shared lock ANTES de mutar. No-op en SQLite.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)

    entities = [entity_filter] if entity_filter else ["sale", "expense"]
    records = (
        (
            await session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tenant_id,
                    UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
                    UnclassifiedRecord.suggested_entity.in_(entities),
                )
            )
        )
        .scalars()
        .all()
    )
    counts = {"imported_sales": 0, "imported_expenses": 0, "skipped": 0, "needs_manual": 0}
    if not records:
        return counts

    _by_sku, _by_name, _by_token = await _load_product_index(session, tenant_id)
    # F2-T5: índices de identidad solo para el tier de barcode del link.
    _identity_indexes = await _load_product_identity_indexes(session, tenant_id)
    _vertical = await _load_tenant_vertical(session, tenant_id)
    _flush_every = 500

    for i, rec in enumerate(records):
        # B1: idempotencia anclada en el UnclassifiedRecord (importar el mismo
        # sugerido dos veces = 0 filas nuevas). El registro queda IMPORTED igual,
        # pero la huella protege ante reintentos concurrentes / estados sucios.
        if await _register_import_row_fingerprint(
            session,
            tenant_id,
            f"{tenant_id}:{_IMPORT_ROW_ACTION}:unclassified:{rec.id}",
        ):
            counts["skipped"] += 1
            continue
        row: dict[str, Any] = rec.row_data or {}
        fecha = _parse_date(_row_val(row, _FECHA_COLS))
        amount = (
            _parse_amount(_row_val(row, _VENTA_TOTAL_COLS))
            or _parse_amount(_row_val(row, _GASTO_AMOUNT_COLS))
            or _parse_amount(_row_val(row, _VENTA_AMOUNT_COLS))
        )
        if fecha is None or amount is None:
            # F6-A5: no parseó (fecha o monto ilegible) → requiere completarlo a
            # mano en el modal. Distinto de "ya importado" (skipped): la UI debe
            # poder decir cuántos exigen atención del usuario.
            counts["needs_manual"] += 1
            continue
        desc = _clean_str(_row_val(row, _NOMBRE_COLS), 499)
        pay_raw = _clean_str(_row_val(row, _PAGO_COLS), 30)
        product_id = _resolve_product(
            _by_sku,
            _by_name,
            _clean_str(_row_val(row, _NOMBRE_COLS), 299),
            _clean_str(_row_val(row, _SKU_COLS), 99),
            _by_token,
            by_barcode=_identity_indexes.by_barcode,
            barcode=_clean_str(_row_val(row, _BARCODE_COLS), 64),
        )

        if rec.suggested_entity == "sale":
            qty = 1
            qty_raw = _row_val(row, _CANTIDAD_COLS)
            if qty_raw not in (None, "", "None", "nan"):
                try:
                    qty = max(1, int(float(str(qty_raw))))
                except (ValueError, TypeError):
                    qty = 1
            session.add(
                SaleEntry(
                    tenant_id=tenant_id,
                    amount=amount,
                    quantity=qty,
                    transaction_date=fecha,
                    payment_method=normalize_payment_method(pay_raw) if pay_raw else "cash",
                    product_id=product_id,
                    notes=desc or "Importado desde Otros",
                    provenance="REAL",
                    source_upload_id=rec.uploaded_file_id,
                )
            )
            counts["imported_sales"] += 1
        else:
            cat_code, cat_label, _ = classify_expense_with_vertical(
                _clean_str(_row_val_categoria(row), 99), _vertical
            )
            expense = ExpenseEntry(
                tenant_id=tenant_id,
                amount=amount,
                category=cat_code,
                transaction_date=fecha,
                description=desc or "Gasto importado",
                is_recurring=False,
                payment_method=normalize_payment_method(pay_raw) if pay_raw else "transfer",
                provenance="REAL",
                source_upload_id=rec.uploaded_file_id,
            )
            expense.product_id = product_id
            expense.expense_type = infer_expense_type(cat_code, product_id=product_id)
            if cat_label:
                expense.custom_fields = {"category_label": cat_label}
            session.add(expense)
            counts["imported_expenses"] += 1

        rec.status = UNCLASSIFIED_STATUS_IMPORTED
        rec.resolved_at = datetime.now(UTC)
        if (i + 1) % _flush_every == 0:
            await session.flush()

    await session.flush()
    # Import masivo de "Otros": ventas reconstruidas sin cliente → sentinela "Local".
    from app.application.services.customer_sentinel import (  # noqa: PLC0415
        assign_orphan_sales_to_local,
    )

    await assign_orphan_sales_to_local(session, tenant_id)
    return counts


def default_confirmed_fields(summary: dict[str, Any]) -> dict[str, bool]:
    inferred = summary.get("inferred_type")
    return {
        "ventas": bool(summary.get("ventas_detectadas")) or inferred == "ventas",
        "gastos": bool(summary.get("gastos_detectados")) or inferred == "gastos",
        "productos": bool(summary.get("stock_detectado")) or inferred == "stock",
        # F7c: maestros de clientes/proveedores.
        "clientes": bool(summary.get("clientes_detectados")) or inferred == "clientes",
        "proveedores": bool(summary.get("proveedores_detectados")) or inferred == "proveedores",
    }


# ── FASE 4: remito (recepción de mercadería) ──────────────────────────────────


class ReceiptLine:
    """Una línea de remito ya validada (producto + cantidad + costo unitario)."""

    __slots__ = ("product_name", "sku", "qty", "unit_price")

    def __init__(
        self, product_name: str, sku: str | None, qty: int, unit_price: Decimal
    ) -> None:
        self.product_name = product_name
        self.sku = sku
        self.qty = qty
        self.unit_price = unit_price


async def import_receipt(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    lines: list[ReceiptLine],
    *,
    shipping_cost: Decimal | None = None,
    transaction_date: datetime | None = None,
    source_upload_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Registra un remito ya validado: por cada línea, producto (alta/stock) +
    ``ExpenseEntry`` COGS; el envío → un ``ExpenseEntry`` OPEX ``LOGISTICS``.

    El ``supplier_id`` ya está resuelto por el endpoint (proveedor REAL del path,
    nunca sentinela — un remito siempre identifica proveedor). Reutiliza los
    helpers del import (``_resolve_product``/``_ensure_product_for_purchase``,
    ``_apply_purchase_to_stock``). Transacción única (el commit lo hace el caller);
    fail-closed. Devuelve un summary de lo creado.
    """
    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

    # F3-T3: el remito crea productos/stock por fuera de ProductRepository.save.
    # Shared lock ANTES de mutar. No-op en SQLite.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)

    tx_date = transaction_date or now_ar_naive()
    # F2-T4: ``_resolve_product`` normaliza canónicamente (accent/dash-aware),
    # compartido con el import; un remito es human-in-the-loop y SIEMPRE crea el
    # producto incompleto si no resuelve.
    by_sku, by_name, by_token = await _load_product_index(session, tenant_id)
    balance_index = await _load_balance_index(session, tenant_id)
    product_cache: dict[uuid.UUID, Any] = {}

    created_product_ids: list[str] = []
    expense_ids: list[str] = []
    total_cogs = Decimal("0")

    for line in lines:
        # Resolver producto del catálogo; si no existe, crearlo incompleto.
        product_id = _resolve_product(by_sku, by_name, line.product_name, line.sku, by_token)
        if product_id is None:
            product_id = await _ensure_product_for_purchase(
                session,
                tenant_id,
                line.product_name,
                line.sku,
                line.unit_price,
                by_sku,
                by_name,
                by_token,
                product_cache=product_cache,
            )
            if product_id is not None:
                created_product_ids.append(str(product_id))
        line_total = (line.unit_price * line.qty).quantize(Decimal("0.01"))
        total_cogs += line_total
        expense = ExpenseEntry(
            tenant_id=tenant_id,
            amount=line_total,
            category="INVENTORY",
            expense_type="COGS",
            transaction_date=tx_date,
            description=f"Remito: {line.product_name}"[:500],
            is_recurring=False,
            payment_method="transfer",
            provenance="REAL",
            supplier_id=supplier_id,
            product_id=product_id,
            source_upload_id=source_upload_id,
        )
        session.add(expense)
        await session.flush()  # materializar el id del gasto para el summary
        expense_ids.append(str(expense.id))
        # Suma stock + movimiento de inventario (hereda supplier_id del gasto).
        await _apply_purchase_to_stock(
            session,
            tenant_id,
            expense,
            line.qty,
            line.unit_price,
            balance_index=balance_index,
            product_cache=product_cache,
            # A2: el movimiento de un remito lleva origen ``receipt`` (no import).
            source_type=SOURCE_RECEIPT,
        )

    shipping_expense_id: str | None = None
    if shipping_cost is not None and shipping_cost > 0:
        # El envío es OPEX LOGISTICS: mismo proveedor, sin producto ni stock.
        shipping = ExpenseEntry(
            tenant_id=tenant_id,
            amount=shipping_cost.quantize(Decimal("0.01")),
            category="LOGISTICS",
            expense_type="OPEX",
            transaction_date=tx_date,
            description="Remito: costo de envío"[:500],
            is_recurring=False,
            payment_method="transfer",
            provenance="REAL",
            supplier_id=supplier_id,
            product_id=None,
            source_upload_id=source_upload_id,
        )
        session.add(shipping)
        await session.flush()
        shipping_expense_id = str(shipping.id)

    return {
        "lines": len(lines),
        "products_created": created_product_ids,
        "expense_ids": expense_ids,
        "shipping_expense_id": shipping_expense_id,
        "total_cogs_ars": str(total_cogs),
    }
