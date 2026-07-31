"""ColumnMappingService: sugerencias de mapeo de columnas + aprendizaje por tenant."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger

logger = get_logger(__name__)

# ── Campos canónicos por entity_type ─────────────────────────────────────────
CANONICAL_FIELDS: dict[str, dict[str, str]] = {
    "sale": {
        "amount": "Monto de venta",
        "transaction_date": "Fecha de venta",
        "quantity": "Cantidad",
        # Precio realmente vendido en esta transacción. NO se deriva de
        # amount/quantity (ver models/transaction.py).
        "unit_price": "Precio unitario vendido",
        "payment_method": "Método de pago",
        "product_name": "Nombre del producto",
        "notes": "Notas",
        # F7a: campos de referencia al cliente (aditivo — el mapeo/vinculación real
        # a un Customer existente queda para 7c; acá solo se abre el contrato).
        # Prefijo "Cliente — " a propósito: en un select largo agrupa visualmente
        # los campos de referencia. Es el label que ya estaba en la UI antes de
        # que el catálogo pasara a servirse desde acá.
        "customer_dni": "Cliente — DNI",
        "customer_cuit": "Cliente — CUIT",
        "customer_email": "Cliente — Email",
        "customer_phone": "Cliente — Teléfono",
        "customer_name": "Cliente — Nombre",
    },
    "expense": {
        "amount": "Monto del gasto",
        "expense_date": "Fecha del gasto",
        "category": "Categoría",
        "payment_method": "Método de pago",
        "is_recurring": "Recurrente",
        "supplier_name": "Proveedor",
        "notes": "Notas",
        # F7a: campos de referencia al proveedor (aditivo, ver nota de sale arriba).
        # Ver la nota de los campos de cliente: mismo criterio de agrupación.
        "supplier_cuil": "Proveedor — CUIL",
        "supplier_email": "Proveedor — Email",
        "supplier_phone": "Proveedor — Teléfono",
    },
    # F7a: maestro de CLIENTES — campos que persiste el modelo Customer.
    "customer": {
        "customer_type": "Tipo (persona/empresa)",
        "name": "Nombre",
        "last_name": "Apellido",
        "doc_type": "Tipo de documento",
        "dni": "DNI",
        "cuit": "CUIT",
        "iva_condition": "Condición de IVA",
        "email": "Email",
        "phone": "Teléfono",
        "address": "Dirección",
        "locality": "Localidad",
        "province": "Provincia",
        "postal_code": "Código postal",
        "birthday": "Cumpleaños",
        "notes": "Notas",
    },
    # F7a: maestro de PROVEEDORES — ACOTADO a lo que persiste el modelo Supplier
    # HOY (models/supplier.py). No se agregan doc_type/address/locality/province/
    # postal_code/iva_condition: el modelo no los tiene, quedan fuera de esta PR.
    "supplier": {
        "name": "Nombre",
        "last_name": "Apellido",
        "cuil": "CUIL",
        "payment_method": "Método de pago",
        "email": "Email",
        "phone": "Teléfono",
        "notes": "Notas",
    },
    "product": {
        "sku": "Código (SKU)",
        "barcode": "Código de barras (EAN/UPC)",
        "name": "Nombre",
        # Los tres precios son conceptos distintos y coexisten — ver la nota en
        # models/product.py. El precio REALMENTE vendido no es ninguno de estos:
        # va en sale.unit_price.
        "sale_price_ars": "Precio de venta",
        "list_price_ars": "Precio de lista (sugerido)",
        "unit_cost_ars": "Costo unitario",
        "stock_units": "Stock (unidades)",
        "category": "Categoría",
        "description": "Descripción",
        "acquired_at": "Fecha de alta/adquisición",
        "expiry_date": "Fecha de vencimiento",
    },
}

# Campos mínimos requeridos por entity_type
REQUIRED_FIELDS: dict[str, list[str]] = {
    "sale": ["amount", "transaction_date"],
    "expense": ["amount", "expense_date"],
    "product": ["name"],
    "customer": ["name"],
    "supplier": ["name"],
}

# ── Heurísticas: entity_type → target_field → keywords (substring match) ─────
_HEURISTICS: dict[str, dict[str, set[str]]] = {
    "sale": {
        "amount": {
            "precio_venta",
            "venta",
            "ventas",
            "ingreso",
            "monto",
            "importe",
            "total_venta",
            "total_cobrado",
            "cobro",
            "total",
            "valor",
        },
        "transaction_date": {"fecha", "date", "dia", "mes", "periodo"},
        "quantity": {"cantidad", "qty", "unidades", "cant", "items", "unidad"},
        # Precio REALMENTE vendido en esta fila (≠ `amount`, que es el total de la
        # venta, y ≠ `Product.sale_price_ars`, que es el vigente configurado).
        "unit_price": {
            "precio_unitario",
            "p_unitario",
            "precio_vendido",
            "precio_unidad",
            "unitario",
        },
        "payment_method": {"metodo", "medio", "pago", "forma_pago", "payment"},
        "product_name": {
            "producto",
            "descripcion",
            "nombre",
            "articulo",
            "item",
            "name",
            "concepto",
            "detalle",
        },
        "notes": {"notas", "observaciones", "obs", "comentarios", "nota", "memo"},
        # F7a: referencia al cliente (aditivo). Bare "cliente" es seguro acá — no
        # colisiona con ningún keyword existente de sale (ver product_name arriba,
        # que usa "nombre" pero no "cliente").
        "customer_dni": {"dni_cliente", "cliente_dni", "dni"},
        "customer_cuit": {"cuit_cliente", "cliente_cuit", "cuit"},
        "customer_email": {"email_cliente", "cliente_email", "email", "correo", "mail"},
        "customer_phone": {
            "telefono_cliente", "cliente_telefono", "telefono", "teléfono", "whatsapp_cliente",
        },
        "customer_name": {"cliente", "nombre_cliente", "cliente_nombre"},
    },
    "expense": {
        "amount": {
            "costo",
            "gasto",
            "gastos",
            "egreso",
            "compra",
            "pago",
            "monto",
            "importe",
            "total",
            "valor",
        },
        "expense_date": {"fecha", "date", "dia", "mes", "periodo"},
        "category": {"categoria", "tipo", "rubro", "clasificacion", "concepto"},
        "payment_method": {
            "forma_pago",
            "forma_de_pago",
            "metodo_pago",
            "metodo_de_pago",
            "medio_pago",
            "medio_de_pago",
            "tipo_pago",
            "payment",
        },
        "is_recurring": {"recurrente", "recurring", "es_fijo", "frecuencia"},
        "supplier_name": {
            "proveedor",
            "proveedor_nombre",
            "empresa",
            "nombre_proveedor",
            "supplier",
        },
        "notes": {"notas", "observaciones", "descripcion", "detalle", "obs"},
        # F7a: referencia al proveedor (aditivo). "supplier_name" ya existía arriba
        # (no se duplica); acá solo se suman los campos que faltaban.
        "supplier_cuil": {"cuil_proveedor", "proveedor_cuil", "cuil"},
        "supplier_email": {"email_proveedor", "proveedor_email", "email", "correo", "mail"},
        "supplier_phone": {
            "telefono_proveedor", "proveedor_telefono", "telefono", "teléfono",
        },
    },
    # F7a: maestro de CLIENTES (identidad fiscal/contacto — sin datos transaccionales).
    "customer": {
        "customer_type": {"tipo_cliente", "persona_empresa", "tipo"},
        "name": {"nombre", "cliente", "razon_social", "razón_social"},
        "last_name": {"apellido"},
        "doc_type": {"tipo_documento", "tipo_doc"},
        "dni": {"dni"},
        "cuit": {"cuit"},
        "iva_condition": {"condicion_iva", "condición_iva", "situacion_iva", "iva"},
        "email": {"email", "correo", "mail"},
        "phone": {"telefono", "teléfono", "celular", "whatsapp"},
        "address": {"direccion", "dirección", "domicilio"},
        "locality": {"localidad", "ciudad"},
        "province": {"provincia"},
        "postal_code": {"codigo_postal", "código_postal", "cp"},
        "birthday": {"cumpleanos", "cumpleaños", "fecha_nacimiento", "nacimiento"},
        "notes": {"notas", "observaciones", "obs", "comentarios"},
    },
    # F7a: maestro de PROVEEDORES — acotado a los campos que persiste el modelo
    # Supplier hoy (ver CANONICAL_FIELDS["supplier"] arriba).
    "supplier": {
        "name": {"nombre", "proveedor", "razon_social", "razón_social"},
        "last_name": {"apellido"},
        "cuil": {"cuil"},
        "payment_method": {
            "forma_pago", "forma_de_pago", "medio_pago", "condicion_pago", "payment",
        },
        "email": {"email", "correo", "mail"},
        "phone": {"telefono", "teléfono", "celular", "whatsapp", "contacto"},
        "notes": {"notas", "observaciones", "obs", "comentarios"},
    },
    "product": {
        "sku": {"sku", "codigo", "código", "code", "ref", "id_producto"},
        # Tokens distintivos de código de barras. "codigo_de_barras" (más largo)
        # le gana a "codigo" de sku en el desempate por longitud de _heuristic_match.
        "barcode": {
            "barcode", "ean", "upc", "gtin", "barras",
            "codigo_de_barras", "cod_barra", "codigo_barra",
        },
        "name": {
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
        },
        # Los tres precios de un catálogo son campos DISTINTOS. Los keywords
        # largos y específicos ("precio_compra", "precio_lista",
        # "precio_venta_final") le ganan al genérico "precio" gracias a
        # `_match_key`, que colapsa las preposiciones antes de comparar.
        "sale_price_ars": {
            "precio_venta",
            "precio",
            "price",
            "p_venta",
            "venta",
            "precio_venta_final",
            "venta_final",
            "precio_final",
        },
        "list_price_ars": {
            "lista",
            "precio_lista",
            "sugerido",
            "precio_sugerido",
            "precio_venta_sugerido",
            "pvp",
        },
        # "precio unitario" en un CATÁLOGO es el costo al que se compra la unidad
        # (no el precio al que se vende: ese es `sale_price_ars`). En una hoja de
        # VENTAS el mismo header significa lo vendido y va a `sale.unit_price`.
        "unit_cost_ars": {
            "costo",
            "cost",
            "precio_costo",
            "p_costo",
            "costo_unitario",
            "compra",
            "precio_compra",
            "costo_compra",
            "precio_unitario",
            "p_unitario",
            "unitario",
        },
        "stock_units": {
            "stock",
            "cantidad",
            "inventario",
            "units",
            "qty",
            "existencia",
            "unidades",
        },
        "category": {"categoria", "tipo", "rubro"},
        "description": {"descripcion", "descripción", "detalle", "comentarios"},
        # F6-B1: fechas de producto. La palabra genérica "fecha" NO auto-mapea
        # ninguna (evita robarle la columna de fecha de venta/gasto en hojas mixtas).
        "acquired_at": {
            "alta", "adquisicion", "adquisición",
            "fecha_alta", "fecha_ingreso", "fecha_compra",
        },
        "expiry_date": {
            "vencimiento", "caducidad", "vence", "vto",
            "expira", "expiracion", "expiración",
        },
    },
}


def _normalize_col(col: str) -> str:
    """Normalizar header para matching: lowercase + underscore.

    NO tocar sin migrar datos: este es el valor que se persiste en
    ``tenant_column_mappings.source_column`` (el historial de alias aprendidos por
    cada tenant). Cambiar la forma normalizada dejaría huérfano todo lo aprendido.
    Para ajustar el matching heurístico está ``_match_key``, que deriva de acá y
    NO se persiste.
    """
    return col.lower().strip().replace(" ", "_").replace("-", "_")


# Preposiciones y artículos que no aportan al matching. "Precio de compra" y
# "Precio compra" son el mismo header para una heurística; escribir las dos
# variantes en cada set de keywords sería inmantenible.
_STOPWORDS: frozenset[str] = frozenset({"de", "del", "la", "el", "los", "las", "por"})


def _match_key(normalized: str) -> str:
    """Clave de matching heurístico: el header normalizado sin preposiciones.

    Existe por un empate real. ``_heuristic_match`` gana con el keyword MÁS LARGO
    y solo reemplaza si es estrictamente mayor, así que sobre ``precio_de_compra``
    los keywords ``precio`` (6, ``sale_price_ars``) y ``compra`` (6,
    ``unit_cost_ars``) empataban y ganaba el primero que se iterara — el costo de
    compra entraba como precio de venta (incidente ASTERIA). Con la clave
    ``precio_compra`` el keyword ``precio_compra`` (13) le gana a ``precio`` (6) y
    el desempate deja de depender del orden de un dict.

    Deliberadamente NO se toca ``_normalize_col``: esa alimenta el historial
    persistido por tenant.
    """
    parts = [p for p in normalized.split("_") if p and p not in _STOPWORDS]
    # Un header que sea SOLO stopwords ("de") dejaría la clave vacía; se devuelve
    # el original antes que una cadena vacía.
    return "_".join(parts) or normalized


# Los mismos keywords ya pasados por `_match_key`, precomputados al importar: el
# matching compara clave contra clave, así un keyword escrito "forma_de_pago"
# sigue matcheando un header "forma pago" sin tener que declarar las dos formas.
_HEURISTIC_KEYS: dict[str, dict[str, frozenset[str]]] = {
    entity: {
        target: frozenset(_match_key(k) for k in keywords) for target, keywords in targets.items()
    }
    for entity, targets in _HEURISTICS.items()
}


# ── Campos de valor único ────────────────────────────────────────────────────
# Un campo escalar solo puede venir de UNA columna. Si dos apuntan al mismo, el
# importador se quedaba con la primera del orden del archivo y descartaba el
# resto en silencio (`_resolve_target_cols`): elegir un dato de negocio por un
# detalle de implementación es inventarlo. El confirm ahora lo rechaza y la UI lo
# bloquea, las dos leyendo de acá.
#
# Alcance deliberado: montos, cantidades, fechas y los tres precios — donde una
# colisión corrompe plata. `name`/`notes`/`category` quedan afuera (varias
# columnas pueden ser legítimas) y se cubren con un aviso no bloqueante.
SINGLE_VALUE_FIELDS: dict[str, frozenset[str]] = {
    "sale": frozenset({"amount", "quantity", "transaction_date", "unit_price"}),
    "expense": frozenset({"amount", "expense_date"}),
    "product": frozenset(
        {"sale_price_ars", "list_price_ars", "unit_cost_ars", "stock_units"}
    ),
    "customer": frozenset(),
    "supplier": frozenset(),
}


# Targets canónicos que representan la fecha de negocio de una fila.
_DATE_TARGET_FIELDS: frozenset[str] = frozenset({"transaction_date", "expense_date"})


def resolve_transaction_date_column(
    headers: list[str] | None,
    mappings: dict[str, str] | None,
) -> str | None:
    """Fuente ÚNICA de verdad: ¿esta hoja/entidad tiene una columna de fecha
    resoluble? Devuelve el nombre de columna, o ``None`` si no hay ninguna.

    Precedencia idéntica a la del importador (F6-A1): primero el mapeo explícito
    (``source_col`` cuyo ``target`` sea ``transaction_date``/``expense_date``),
    luego la heurística por substring del header contra ``FECHA_COLS`` — el mismo
    criterio que ``file_parsing.has_fecha`` y que ``_find_col(headers, FECHA_COLS)``.
    Sin esta función, la API y el importador terminaban divergiendo sobre "esta
    hoja tiene fecha o no" (ver C1).
    """
    if mappings:
        for src, target in mappings.items():
            # El mapeo explícito solo vale si la columna EXISTE en la hoja. Un
            # payload viejo/inconsistente como {"col_inexistente": "transaction_date"}
            # pasaría el gate, pero el importador obtendría None por fila (la columna
            # no está) y mandaría todo a /otros — el 422-antes-del-lease se saltearía.
            if target in _DATE_TARGET_FIELDS and (headers is None or src in headers):
                return src
    if headers:
        # Import local: file_parsing es un módulo pesado; column_mapping_service se
        # importa desde routers livianos. FECHA_COLS es el set canónico de keywords.
        from app.application.services.file_parsing import FECHA_COLS  # noqa: PLC0415

        for h in headers:
            norm = _normalize_col(h)
            if any(k in norm for k in FECHA_COLS):
                return h
    return None


def validate_required_date_mapping(
    included: list[tuple[str, list[str] | None, dict[str, str]]],
) -> list[str]:
    """F6-A1: dado un conjunto de contextos venta/gasto INCLUIDOS en el import,
    devuelve las etiquetas de los que NO tienen columna de fecha resoluble.

    Cada elemento de ``included`` es ``(label, headers, mappings)``. La API arma
    la lista desde su estado de confirmación (flat vs por-contexto) y esta función
    aplica el mismo resolver que el importador — sin que el router toque
    ``FECHA_COLS`` ni ``_find_col`` (ambos privados del pipeline de import).

    Lista vacía = todos los contextos incluidos tienen fecha.
    """
    missing: list[str] = []
    for label, headers, mappings in included:
        if resolve_transaction_date_column(headers, mappings) is None:
            missing.append(label)
    return missing


def _heuristic_match(normalized: str, entity_type: str) -> str | None:
    """Busca el target_field para un header normalizado.

    1. Match exacto (gana siempre), contra el header crudo y contra su
       ``_match_key`` — así "Precio de compra" y "Precio compra" resuelven igual
       sin duplicar cada keyword.
    2. Substring sobre la clave: gana el keyword MÁS LARGO entre todos los campos
       — evita que un keyword corto y genérico de otro campo capture un header
       específico (ej: `forma_pago` debe ir a payment_method por "forma_pago", no
       a amount por el substring "pago").

    Ante un empate de longitud gana el primero declarado en ``_HEURISTICS``. Eso
    ya no puede corromper un campo escalar en silencio: la colisión se valida
    aguas arriba (``SINGLE_VALUE_FIELDS``) y el confirm la rechaza.
    """
    heuristics = _HEURISTICS.get(entity_type, {})
    keyed = _HEURISTIC_KEYS.get(entity_type, {})
    key = _match_key(normalized)
    for target_field, keywords in heuristics.items():
        if normalized in keywords or key in keyed.get(target_field, frozenset()):
            return target_field
    best_len = 0
    best_target: str | None = None
    for target_field, keywords_k in keyed.items():
        for k in keywords_k:
            if len(k) > best_len and k in key:
                best_len = len(k)
                best_target = target_field
    return best_target


def _fuzzy_match(normalized: str, entity_type: str) -> tuple[str | None, float]:
    """Similitud fuzzy entre nombre normalizado y keywords. Retorna (target_field, ratio)."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None, 0.0

    heuristics = _HEURISTICS.get(entity_type, {})
    best_target: str | None = None
    best_ratio = 0.0
    for target_field, keywords in heuristics.items():
        for kw in keywords:
            ratio = fuzz.ratio(normalized, kw) / 100.0
            if ratio > best_ratio:
                best_ratio = ratio
                best_target = target_field
    if best_ratio >= 0.70:
        return best_target, best_ratio
    return None, 0.0


class ColumnMappingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def suggest_mappings(
        self,
        tenant_id: uuid.UUID,
        entity_type: str,
        headers: list[str],
        sample_rows: list[dict[str, Any]],
        *,
        trace_id: uuid.UUID | str | None = None,
        file_id: uuid.UUID | str | None = None,
        allow_llm: bool = True,
    ) -> list[dict[str, Any]]:
        """Genera sugerencias de mapeo para los headers del archivo.

        FASE 2 (A2): si `trace_id` y `file_id` están presentes, la decisión de la
        4ª capa LLM se traza en pipeline_events (stage="mapping"). Los parámetros
        son keyword-only y opcionales para no romper los callers existentes.

        ``allow_llm=False`` (F7d review) saltea por completo la 4ª capa LLM —
        para callers de solo-lectura/idempotentes (ej. el preview de maestros de
        ``GET /files/{id}/preview``, que puede correr en cada poll/reload) que NO
        deben disparar una llamada real al LLM aunque ``ENABLE_LLM_COLUMN_MAPPING``
        esté prendido. El flujo real de mapeo (``GET /column-mappings``, que el
        usuario dispara explícitamente al armar el mapeo) sigue con el default
        ``True``.
        """
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        # Cargar historial del tenant para este entity_type
        result = await self.db.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == tenant_id,
                TenantColumnMapping.entity_type == entity_type,
            )
        )
        history: dict[str, TenantColumnMapping] = {
            row.source_column: row for row in result.scalars().all()
        }

        required = set(REQUIRED_FIELDS.get(entity_type, []))
        suggestions: list[dict[str, Any]] = []

        for header in headers:
            normalized = _normalize_col(header)

            # Extraer sample values (hasta 5 no-nulos)
            sample_vals: list[str] = []
            for row in sample_rows[:10]:
                v = row.get(header)
                if v is not None and str(v).strip() not in ("", "None", "nan"):
                    sample_vals.append(str(v)[:50])
                if len(sample_vals) >= 5:
                    break

            target_field: str | None = None
            confidence: float = 0.0
            source: str = "none"

            # 1. Historial del tenant (prioridad máxima)
            if normalized in history:
                rec = history[normalized]
                target_field = rec.target_field
                confidence = min(0.99, 0.5 + rec.confirmed_count / 20.0)
                source = "tenant_history"

            # 2. Heurística global
            elif (heuristic := _heuristic_match(normalized, entity_type)) is not None:
                target_field = heuristic
                confidence = 0.75
                source = "heuristic"

            # 3. Fuzzy matching
            else:
                fuzzy_target, fuzzy_ratio = _fuzzy_match(normalized, entity_type)
                if fuzzy_target is not None:
                    target_field = fuzzy_target
                    confidence = fuzzy_ratio * 0.65  # escalar a rango 0–65%
                    source = "fuzzy"

            # Calcular status
            if target_field is not None and target_field != "ignore":
                status = "mapped"
            else:
                status = "unmapped"

            suggestions.append(
                {
                    "source_column": header,
                    "normalized_column": normalized,
                    "sample_values": sample_vals,
                    "target_field": target_field,
                    "confidence": round(confidence, 3),
                    "source": source,
                    "status": status,
                }
            )

        # FASE 2: 4ª capa LLM (fallback). Solo para columnas con baja confianza
        # determinística. Una sola llamada batch; fail-silent (flag/key/errores).
        # `allow_llm=False` la saltea por completo (ver docstring de este método).
        if allow_llm:
            await self._apply_llm_fallback(
                entity_type,
                suggestions,
                tenant_id=tenant_id,
                trace_id=trace_id,
                file_id=file_id,
            )

        # Segunda pasada: detectar required_missing
        mapped_targets = {
            s["target_field"] for s in suggestions if s["status"] == "mapped"
        }
        missing_required = required - mapped_targets

        # Si hay campos requeridos sin cubrir, marcar la primera columna sin mapear
        # cuyo nombre normalizado se acerque a algún campo requerido
        if missing_required:
            for s in suggestions:
                if s["status"] == "unmapped":
                    norm = s["normalized_column"]
                    for req_field in list(missing_required):
                        # Check si algún keyword del required field está en el nombre
                        req_keywords = _HEURISTICS.get(entity_type, {}).get(req_field, set())
                        if any(k in norm for k in req_keywords):
                            s["status"] = "required_missing"
                            missing_required.discard(req_field)
                            break

        return suggestions

    async def _apply_llm_fallback(
        self,
        entity_type: str,
        suggestions: list[dict[str, Any]],
        *,
        tenant_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | str | None = None,
        file_id: uuid.UUID | str | None = None,
    ) -> None:
        """FASE 2: mejora las sugerencias de baja confianza con el LLM (in-place).

        Si `trace_id`/`file_id` están presentes, emite un pipeline_event con la
        traza antes/después de cada columna evaluada (qué decidió lo determinístico,
        qué decidió el LLM, y si lo pisó).
        """
        from app.application.services.llm_column_mapper import (  # noqa: PLC0415
            LLM_MAPPING_THRESHOLD,
            suggest_with_llm,
        )

        low_conf = [s for s in suggestions if s["confidence"] < LLM_MAPPING_THRESHOLD]
        if not low_conf:
            return
        valid_fields = CANONICAL_FIELDS.get(entity_type, {})
        if not valid_fields:
            return

        # Snapshot "antes" (la decisión determinística) para auditar qué pisó el LLM.
        before = {
            s["source_column"]: {
                "target_field": s["target_field"],
                "confidence": s["confidence"],
                "source": s["source"],
            }
            for s in low_conf
        }

        llm_result = await suggest_with_llm(
            entity_type,
            [{"header": s["source_column"], "sample_values": s["sample_values"]} for s in low_conf],
            valid_fields,
        )
        if not llm_result:
            return

        decisions: list[dict[str, Any]] = []
        for s in low_conf:
            hit = llm_result.get(s["source_column"])
            prev = before[s["source_column"]]
            overwritten = False
            if hit:
                target = hit["target_field"]
                conf = hit["confidence"]
                # Solo pisar si el LLM aporta un mapeo usable y MÁS confiable.
                if target != "ignore" and conf > s["confidence"]:
                    s["target_field"] = target
                    s["confidence"] = round(conf, 3)
                    s["source"] = "llm"
                    s["status"] = "mapped"
                    overwritten = True
            decisions.append(
                {
                    "column": s["source_column"],
                    "deterministic_target": prev["target_field"],
                    "deterministic_confidence": prev["confidence"],
                    "source_before": prev["source"],
                    "llm_target": hit["target_field"] if hit else None,
                    "llm_confidence": hit["confidence"] if hit else None,
                    "source_after": s["source"],
                    "final_target": s["target_field"],
                    "final_confidence": s["confidence"],
                    "overwritten": overwritten,
                }
            )

        await self._emit_mapping_event(
            tenant_id=tenant_id,
            trace_id=trace_id,
            file_id=file_id,
            entity_type=entity_type,
            decisions=decisions,
        )

    async def _emit_mapping_event(
        self,
        *,
        tenant_id: uuid.UUID | None,
        trace_id: uuid.UUID | str | None,
        file_id: uuid.UUID | str | None,
        entity_type: str,
        decisions: list[dict[str, Any]],
    ) -> None:
        """Traza la decisión del LLM de mapeo en pipeline_events (fail-silent).

        No emite si falta `trace_id`/`file_id`/`tenant_id` (callers que no pasan
        contexto de traza, p.ej. tests unitarios del mapeo)."""
        if trace_id is None or file_id is None or tenant_id is None:
            return
        from app.application.services import pipeline_event_service  # noqa: PLC0415
        from app.persistence.models.pipeline_event import STAGE_MAPPING  # noqa: PLC0415

        overwritten_count = sum(1 for d in decisions if d["overwritten"])
        await pipeline_event_service.emit_event(
            self.db,
            trace_id=trace_id,
            tenant_id=tenant_id,
            stage=STAGE_MAPPING,
            file_id=file_id,
            detail={
                "type": "column_mapping",
                "entity_type": entity_type,
                "columns_evaluated": len(decisions),
                "columns_overwritten": overwritten_count,
                "decisions": decisions,
            },
        )

    async def save_mappings(
        self,
        tenant_id: uuid.UUID,
        entity_type: str,
        confirmed: list[dict[str, str]],
    ) -> None:
        """Upsert de mapeos confirmados en tenant_column_mappings.

        No aprende mapeos "ignore" — cada archivo puede tener columnas distintas
        que ignorar. No aprende custom_fields tampoco (demasiado específicos).
        """
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        now = datetime.now(tz=UTC)

        for mapping in confirmed:
            source_col = _normalize_col(mapping["source_column"])
            target = mapping["target_field"]

            # No aprendemos "ignore" ni custom_fields
            if target == "ignore" or target.startswith("custom_field:"):
                continue

            result = await self.db.execute(
                select(TenantColumnMapping).where(
                    TenantColumnMapping.tenant_id == tenant_id,
                    TenantColumnMapping.entity_type == entity_type,
                    TenantColumnMapping.source_column == source_col,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                if existing.target_field != target:
                    # Usuario cambió el mapeo → reiniciar contador
                    existing.target_field = target
                    existing.confirmed_count = 1
                else:
                    existing.confirmed_count += 1
                existing.last_seen_at = now
            else:
                self.db.add(
                    TenantColumnMapping(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        entity_type=entity_type,
                        source_column=source_col,
                        target_field=target,
                        confirmed_count=1,
                        last_seen_at=now,
                        created_at=now,
                    )
                )

        await self.db.flush()
        logger.info(
            "column_mapping.saved",
            tenant_id=str(tenant_id),
            entity_type=entity_type,
            count=len(confirmed),
        )

    async def get_learned_mappings(self, tenant_id: uuid.UUID) -> list[Any]:
        """Retorna todos los mapeos aprendidos del tenant, ordenados por entity_type + source."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        result = await self.db.execute(
            select(TenantColumnMapping)
            .where(TenantColumnMapping.tenant_id == tenant_id)
            .order_by(TenantColumnMapping.entity_type, TenantColumnMapping.source_column)
        )
        return list(result.scalars().all())

    async def delete_mapping(
        self, tenant_id: uuid.UUID, mapping_id: uuid.UUID
    ) -> bool:
        """Elimina un mapeo aprendido. Retorna True si existía."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        result = await self.db.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.id == mapping_id,
                TenantColumnMapping.tenant_id == tenant_id,
            )
        )
        existing = result.scalar_one_or_none()
        if not existing:
            return False
        await self.db.delete(existing)
        await self.db.flush()
        return True
