"""Pydantic schemas for the ingestion pipeline endpoints."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    file_id: UUID
    status: str  # always "PROCESSING" immediately after upload
    # Dedup de re-upload: id de un archivo YA importado con el mismo contenido (si existe).
    duplicate_of: UUID | None = None
    warning: str | None = None


class FileStatusItem(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    purpose: str
    processing_status: str
    created_at: datetime


class ColumnAtRisk(BaseModel):
    column: str
    null_pct: float
    recommendation: str = "drop"


class ContextualColumnRisk(BaseModel):
    """F8a: riesgo de una columna mapeada, POR CONTEXTO y según el mapeo efectivo.

    Reemplaza al diagnóstico global ``ColumnAtRisk`` (basado solo en el nombre de
    columna). Solo son accionables los targets requeridos y los opcionales que el
    usuario seleccionó explícitamente (``user_selected=True``). ``affected_rows`` es
    exacto (vacíos + inválidos según el parser canónico del target).
    """

    context_id: str
    entity_type: str
    source_column: str
    target_field: str
    null_ratio: float  # 0.0–1.0; la UI lo muestra ×100
    affected_rows: int  # vacíos + inválidos, exacto
    null_rows: int
    invalid_rows: int
    field_requirement: Literal["required", "explicitly_selected", "optional"]
    mapping_source: Literal["tenant_history", "heuristic", "fuzzy", "llm", "none"]
    user_selected: bool
    allowed_actions: list[str] = Field(default_factory=list)
    recommendation: str


class MasterPreviewSample(BaseModel):
    """Fila de muestra del preview de un maestro (cliente/proveedor). PII
    minimizada a propósito: solo nombre + estado + primer diagnóstico — nunca
    DNI/CUIT/email/teléfono crudos (esos solo viven en memoria durante el
    request, no se serializan)."""

    row_index: int
    status: str  # "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review"
    display_name: str | None = None
    existing_name: str | None = None
    issue: str | None = None


class MasterPreviewSummary(BaseModel):
    """Preview de una hoja de maestro (F7d) — cuántas filas son create/update/
    needs_review/invalid/duplicate ANTES de confirmar. No persiste nada."""

    context_id: str | None = None
    entity_type: str  # "customer" | "supplier"
    to_create: int
    to_update: int
    needs_review: int
    invalid: int
    duplicates: int
    samples: list[MasterPreviewSample] = Field(default_factory=list)


class PreservedEntity(BaseModel):
    """Una entidad que sobrevive al borrado, y por qué.

    ``fields`` se completa SOLO cuando la decisión es por campo
    (``campo_modificado_posteriormente``): dice cuáles no se restauran, mientras
    el resto de la entidad sí vuelve a su valor anterior.

    ``unclassified`` y ``file`` no son entidades concretas sino AGREGADOS del
    archivo ("N filas de Otros que no se pueden rastrear", "este archivo no dejó
    ledger"). Existen como tipos propios porque reportarlos como ``product`` con
    el id del ARCHIVO —que es lo que se hacía— es un dato falso: la UI no puede
    linkearlo y quien lea el audit log meses después no tiene forma de saber que
    ese uuid no era un producto.
    """

    entity_type: Literal[
        "product", "customer", "supplier", "sale", "expense", "unclassified", "file"
    ]
    id: UUID
    name: str
    reasons: list[str]
    fields: list[str] = []


class FileDeletionPreviewResponse(BaseModel):
    """Qué datos se lleva puestos el borrado de un archivo.

    Alimenta la advertencia que el usuario acepta o cancela. El borrado revierte
    también lo editado a mano, así que ``has_user_edits`` no bloquea: informa.
    """

    file_id: UUID
    ventas: int
    gastos: int
    productos: int
    movimientos_stock: int
    otros: int
    # Filas de "Otros" que el usuario YA clasificó: NO se borran. El registro que
    # generaron (venta/gasto/producto) no lleva `source_upload_id`, así que la
    # reversa no lo alcanza y borrar la fila destruiría su único rastro.
    otros_ya_clasificados: int
    # Hay registros de este archivo que alguien editó a mano después de importar.
    has_user_edits: bool
    # El archivo se importó antes del ledger de reversa: no se puede saber qué
    # productos creó, así que quedan vivos y hay que revisarlos a mano.
    productos_no_rastreables: bool
    # Productos que el archivo MODIFICÓ y a los que se les va a devolver su valor
    # anterior (no se borran: ya existían).
    productos_a_restaurar: int = 0
    # Lo que NO se va a poder revertir, con nombre y motivo. El backend ya sabía
    # calcularlo y lo descartaba en silencio; sin esto el borrado prometía una
    # limpieza que no cumplía.
    conservados: list[PreservedEntity] = []


class FileDeletionResult(BaseModel):
    """Resultado del borrado. Siempre 200, nunca un 204 mudo.

    ``fully_reverted`` es la afirmación que la UI necesita para elegir entre
    "se eliminó todo" y "se eliminó, pero quedaron N cosas". Su valor lo decide el
    DELETE dentro de su transacción — el preview es una estimación previa, no una
    promesa: entre las dos llamadas alguien pudo registrar una venta o editar un
    producto.
    """

    status: Literal["deleted"] = "deleted"
    fully_reverted: bool
    deleted: dict[str, int]
    restored: dict[str, int]
    conservados: list[PreservedEntity] = []


class FilePreviewResponse(BaseModel):
    model_config = {"from_attributes": True}

    file_id: UUID
    processing_status: str
    parsed_summary_json: dict[str, Any] | None
    # Deprecado (diagnóstico global por nombre de columna); ver contextual_column_risk.
    columns_at_risk: list[ColumnAtRisk] = []
    # F8a: riesgo contextual por columna mapeada (informativo en el preview, desde
    # las sugerencias de mapeo). Vacío si el archivo no tiene contextos transaccionales.
    contextual_column_risk: list[ContextualColumnRisk] = Field(default_factory=list)
    # F7d: preview universal de maestros (clientes/proveedores) — vacío si el
    # archivo no tiene hojas de maestro o si no se pudo estimar el mapeo.
    master_previews: list[MasterPreviewSummary] = Field(default_factory=list)


# ── Column mapping schemas ────────────────────────────────────────────────────


class ColumnMappingSuggestion(BaseModel):
    source_column: str
    normalized_column: str
    sample_values: list[str]
    target_field: str | None
    confidence: float
    source: Literal["tenant_history", "heuristic", "fuzzy", "llm", "none"]
    status: Literal["mapped", "unmapped", "required_missing"]
    # Contexto al que pertenece la sugerencia (hoja/tabla). None = archivo de un solo contexto.
    context_id: str | None = None


class FieldCatalogEntry(BaseModel):
    """Un campo canónico al que se puede mapear una columna."""

    value: str
    label: str
    # True = solo UNA columna puede apuntarle. Dos columnas a un campo escalar no
    # se pueden desempatar sin inventar, así que el confirm las rechaza y la UI
    # bloquea. Ver SINGLE_VALUE_FIELDS en column_mapping_service.
    single_value: bool = False


class EntityFieldCatalog(BaseModel):
    """Campos disponibles y requeridos para una entidad."""

    # Un requerido se cubre SOLO con un campo canónico: un `custom_field:` guarda
    # el dato pero no satisface el requerido (misma regla que `_missing_required`).
    required: list[str]
    # F-H4: qué otro conjunto COMPLETO de campos cubre un requerido —
    # `{"amount": ["unit_price", "quantity"]}`. Viaja en el catálogo porque la UI
    # tiene que poder decir exactamente lo mismo que el confirm sobre si una hoja
    # se puede importar; con una copia propia, la pantalla bloquearía un archivo
    # que el backend acepta (o al revés). Vacío para las entidades sin alternativa.
    required_alternatives: dict[str, list[str]] = {}
    fields: list[FieldCatalogEntry]


class InventoryEffectOption(BaseModel):
    """Un modo de inventario ofrecible, con su texto en castellano."""

    value: str
    #: De `EFFECT_LABELS`: describe QUÉ le pasa al stock, no el nombre técnico.
    label: str


class SheetInventoryEffect(BaseModel):
    """F-H3.e — qué propone Véktor para una hoja y entre qué puede elegir el usuario.

    El default y las opciones salen de `domain/inventory_effect` (`default_effect_for`
    / `options_for`), que dependen de la entidad de la hoja y de los campos que el
    mapeo BORRADOR ya cubre. Por eso se calcula del lado del servidor y con el mapeo
    en curso, en vez de una tabla fija en la UI: cambiar una columna a `quantity`
    cambia lo que la hoja puede hacerle al inventario.
    """

    context_id: str
    #: Nombre legible de la hoja (nunca el `context_id` crudo).
    label: str
    default: str
    #: Siempre incluye `default`. Con un solo elemento no hay nada que elegir: la
    #: hoja no habla de unidades y la UI sólo informa el modo.
    options: list[InventoryEffectOption]


class ColumnMapping(BaseModel):
    source_column: str
    target_field: str  # campo canónico, "ignore", o "custom_field:{key}"
    # Mapeo cualificado por contexto (multi-hoja / multi-grupo). None = mapeo plano legacy.
    context_id: str | None = None
    # entity_type del contexto (sale|expense|product|customer|supplier)
    entity_type: str | None = None
    # F8a: el usuario tocó/confirmó/creó este mapping (vs. aceptar pasivamente una
    # sugerencia). Solo True vuelve accionable un target OPCIONAL en el protocolo de
    # riesgo. El backend nunca lo infiere de la mera presencia del mapping.
    user_selected: bool = False


class ColumnRiskRequest(BaseModel):
    """Body del endpoint read-only ``POST /files/{id}/column-risk``: el mapeo
    provisional (draft) que el usuario armó, para recalcular el riesgo con el mapeo
    efectivo (incluye ``user_selected`` por columna). No persiste nada.

    ``confirmed_fields``/``context_confirmed`` espejan el body del confirm: los
    contextos que el usuario decidió NO importar no generan riesgo accionable
    (misma decisión de inclusión que ``POST /confirm``)."""

    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    context_entity: dict[str, Literal["sale", "expense", "product", "customer", "supplier"]] = (
        Field(default_factory=dict)
    )
    confirmed_fields: dict[str, bool] = Field(default_factory=dict)
    context_confirmed: dict[str, bool] = Field(default_factory=dict)


class TenantColumnMappingResponse(BaseModel):
    id: UUID
    entity_type: str
    source_column: str
    target_field: str
    confirmed_count: int
    last_seen_at: datetime


# ── Ingestion confirm ─────────────────────────────────────────────────────────


class ColumnRiskDecision(BaseModel):
    """F8b: decisión del usuario sobre UNA columna riesgosa (F8a) al confirmar.

    ``action`` es un set cerrado de solo dos valores: ``drop_column`` (eliminar
    la columna del mapeo antes de importar) o ``route_affected_rows_to_others``
    (las filas afectadas — vacías/inválidas en esa columna — van a "Otros" en
    vez de importarse con el dato faltante). ``cancel_and_complete`` NO es una
    decisión por columna: es una acción global manejada por ``POST /cancel``."""

    context_id: str
    source_column: str
    target_field: str
    action: Literal["drop_column", "route_affected_rows_to_others"]


class ConfirmIngestionRequest(BaseModel):
    confirmed_fields: dict[str, Any] = Field(
        description=(
            "Which data categories to import from the parsed file. "
            "Keys: 'ventas', 'gastos', 'productos', 'clientes', 'proveedores' (F7 — "
            "clientes/proveedores como entidad de primera clase, ya integrado). "
            "Values: bool."
        )
    )
    column_mappings: list[ColumnMapping] = Field(
        default_factory=list,
        description=(
            "Mapeo explícito de columnas del archivo a campos canónicos del dominio. "
            "Si se omite, el sistema usa heurísticas automáticas. En archivos multi-contexto "
            "(multi-hoja), cada ColumnMapping lleva su context_id + entity_type."
        ),
    )
    context_confirmed: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Inclusión por contexto (sheet/grupo) en archivos multi-contexto: "
            "{context_id: incluir}. Vacío = se usa confirmed_fields por tipo (legacy)."
        ),
    )
    context_entity: dict[str, Literal["sale", "expense", "product", "customer", "supplier"]] = (
        Field(
            default_factory=dict,
            description=(
                "Override de entity_type por contexto en documentos de texto/imagen: "
                "{context_id: sale|expense|product|customer|supplier}. Permite reasignar un "
                "grupo detectado. Un valor inválido/vacío se rechaza acá (422) — nunca cae "
                "silenciosamente a la entidad original vía `or`."
            ),
        )
    )
    stock_treatment: (
        Literal["opening_balance", "purchase"]
        | dict[str, Literal["opening_balance", "purchase"]]
        | None
    ) = Field(
        default=None,
        description=(
            "Cómo tratar el stock de una hoja de catálogo/lista: 'opening_balance' "
            "(saldo de apertura — mercadería que ya tenías, entra al inventario sin "
            "gasto ni salida de caja) o 'purchase' (compra — genera gasto de mercadería "
            "COGS + baja de caja). Si se omite, se asume saldo de apertura.\n\n"
            "Acepta un dict {context_id: tratamiento} para decidir POR HOJA. Un "
            "archivo puede traer un catálogo que el negocio ya tenía y otra hoja de "
            "compras del mes: un único valor global obliga a mentir en una de las dos "
            "y, si se elige 'purchase', genera COGS por productos que ya figuran como "
            "egresos en el libro diario (doble conteo). Un string plano sigue "
            "significando 'para todas las hojas de producto' (compatibilidad)."
        ),
    )
    inventory_effect: (
        dict[
            str,
            Literal["informational", "historical_replay", "current_snapshot", "no_inventory"],
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "F-H3: qué le hace al INVENTARIO cada hoja, como `{context_id: modo}`.\n\n"
            "- `informational`: calcula el impacto y lo reporta, sin tocar stock.\n"
            "- `historical_replay`: las compras suman y las ventas restan.\n"
            "- `current_snapshot`: el archivo declara el saldo absoluto (una foto).\n"
            "- `no_inventory`: la cantidad no habla de inventario.\n\n"
            "Eje SEPARADO de `stock_treatment`, que es contable (¿el stock inicial "
            "del catálogo genera COGS y baja de caja?). Fusionarlos haría que elegir "
            "'las ventas descuentan' declare en silencio que el catálogo genera COGS.\n\n"
            "Si se omite, cada hoja toma su default; el default NUNCA es "
            "`historical_replay`: aplicar el histórico de un archivo que puede estar "
            "incompleto o solaparse con saldos ya cargados es una decisión del "
            "usuario, hoja por hoja. Un modo inválido o una hoja inexistente se "
            "rechazan con 422 en vez de ignorarse."
        ),
    )
    column_risk_decisions: list[ColumnRiskDecision] = Field(
        default_factory=list,
        description=(
            "F8b: decisiones del usuario sobre columnas riesgosas (F8a) detectadas "
            "en el preview/column-risk. Opcional — vacío por default para mantener "
            "compatibilidad con confirms previos (F7) que no conocen este campo."
        ),
    )


class InventoryImpactItem(BaseModel):
    """F-H3.c: qué le PASARÍA al stock de un producto si se aplicara el archivo.

    Nada de esto se aplicó: con el default (`informational`) el import calcula y
    reporta. Los números son los del replay por fecha, no un neto de unidades:
    ``minimo``/``primer_negativo_en`` sólo existen porque se reprodujo la
    secuencia día por día.
    """

    product_id: str
    product_name: str
    #: Saldo ANTES del archivo (o el absoluto que declara un catálogo).
    saldo_inicial: int
    #: Saldo tras reproducir compras y ventas por fecha.
    saldo_final: int
    compradas: int
    vendidas: int
    #: Menor saldo alcanzado durante la secuencia, y cuándo.
    minimo: int
    minimo_en: str | None = None
    #: Primer día en que el saldo se fue abajo de cero. `None` = nunca.
    #: Tocar negativo NO es lo mismo que quedar negativo (`saldo_final < 0`): un
    #: final sano con un pozo en el medio significa que faltan compras viejas.
    primer_negativo_en: str | None = None


class ConfirmIngestionResponse(BaseModel):
    file_id: UUID
    status: str
    message: str
    # Avisos human-in-the-loop tras confirmar: compras sin proveedor (→ sentinela "No
    # identificado"), compras sin producto detallado (stock incompleto), filas a "Otros".
    # No bloquean; el frontend los muestra en un banner para que el usuario revise.
    warnings: list[str] = Field(default_factory=list)
    # F-H3.c: el impacto proyectado sobre el inventario, para MOSTRARLO. Ordenado
    # con los productos que se van a negativo primero. Acotado (ver
    # `inventory_impact_total`): un catálogo de 1258 productos no entra en una
    # respuesta de confirm, y truncar sin decirlo se leería como "esto es todo".
    inventory_impact: list[InventoryImpactItem] = Field(default_factory=list)
    #: Cuántos productos tienen impacto en total, incluidos los que no se listan.
    inventory_impact_total: int = 0


class InventoryReplayRequest(BaseModel):
    """F-H3.d.4: aplicar al inventario la historia de ventas de un archivo."""

    #: Hojas a aplicar. `None` = todas las del archivo. El eje se declara POR HOJA
    #: al confirmar, así que aplicar todo por default sería contradecir la
    #: declaración cuando el libro mezcla hojas con distinto efecto.
    context_ids: list[str] | None = None
    #: `True` = calcular y mostrar sin escribir. El cálculo es el MISMO que el del
    #: apply; lo único que cambia es si se persiste.
    dry_run: bool = False


class PendingSaleItem(BaseModel):
    """Una venta cuyo descuento no se pudo aplicar por falta de stock."""

    sale_id: str
    product_id: str
    product_name: str
    quantity: int
    #: Unidades que había cuando le tocó el turno. Siempre menor que `quantity`.
    disponible: int


class InventoryReplayResponse(BaseModel):
    """Resultado del replay. Los números son los recalculados en esta corrida.

    Nunca son los que devolvió el confirm: entre confirmar y aplicar el stock pudo
    cambiar, y mostrar un número viejo para una operación que escribió otro es el
    error que ya se pagó en el borrado por procedencia.
    """

    file_id: UUID
    dry_run: bool
    #: Ventas cuyo descuento se aplicó en esta corrida.
    aplicadas: int
    #: Ventas que ya estaban descontadas (aplicar de nuevo, o descontadas en vivo).
    #: No son un error: son el no-op idempotente.
    ya_aplicadas: int
    #: Ventas que quedaron pendientes por falta de stock. NO se anulan: la venta ya
    #: está en los libros y anularla cambiaría facturación confirmada. El usuario
    #: carga el inventario que falta y vuelve a aplicar.
    sin_stock: list[PendingSaleItem] = Field(default_factory=list)
    #: Por producto: saldo antes → ventas → saldo después.
    impacto: list[InventoryImpactItem] = Field(default_factory=list)
    hojas: list[str] = Field(default_factory=list)
    #: `False` si alguna venta del archivo no tiene registrada su hoja (importada
    #: antes de que el import la estampara): el alcance real fue el archivo entero.
    alcance_por_hoja: bool = True
    warnings: list[str] = Field(default_factory=list)


# ── Relectura de archivos (REREAD_FILE) ────────────────────────────────────────


class RereadCounts(BaseModel):
    to_update: int
    preserved: int
    new: int
    to_void: int
    # Filas ya importadas (huella presente) que el reimport saltea — ni nuevas ni
    # duplicadas. Default 0 por compatibilidad.
    unchanged: int = 0
    # Impacto estimado en el catálogo de productos.
    products_new: int = 0
    products_restock: int = 0


class RereadPreviewResponse(BaseModel):
    file_id: UUID
    counts: RereadCounts
    legacy_fallback: bool = False
    sample_changes: list[dict[str, Any]] = Field(default_factory=list)


class RereadItem(BaseModel):
    action: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class RereadApplyResponse(BaseModel):
    file_id: UUID
    run_id: UUID
    to_update: int
    preserved: int
    new: int
    voided: int
    inserted: int
    legacy_fallback: bool = False
    items: list[RereadItem] = Field(default_factory=list)
    # F7d: maestros (clientes/proveedores) reaplicados — creados + actualizados.
    clientes: int = 0
    proveedores: int = 0


class RereadApplyStartResponse(BaseModel):
    """El apply corre en background; se devuelve el run para hacer polling."""

    file_id: UUID
    run_id: UUID
    status: str  # "RUNNING"


class RereadRunStatusResponse(BaseModel):
    """Estado del apply en background (polling). ``status``: RUNNING|APPLIED|FAILED."""

    run_id: UUID
    file_id: UUID
    status: str
    to_update: int = 0
    preserved: int = 0
    new: int = 0
    voided: int = 0
    inserted: int = 0
    legacy_fallback: bool = False
    items: list[RereadItem] = Field(default_factory=list)
    error: str | None = None
    # F7d: maestros (clientes/proveedores) reaplicados — creados + actualizados.
    clientes: int = 0
    proveedores: int = 0


class RereadUndoResponse(BaseModel):
    run_id: UUID
    restored: int
    removed: int
    status: str
    # F9b (Task 7): clientes/proveedores/productos que la relectura tocó pero el
    # undo NO restauró porque alguien los editó después de la relectura (política
    # touched-since — nunca pisar una edición manual en silencio).
    not_reverted_entities: list[dict[str, str]] = Field(default_factory=list)
