import { api } from "@/lib/api";
import type { AxiosError } from "axios";

// El preview estima en memoria (sub-segundo) y el undo es acotado; igual se da
// margen sobre el timeout global de 15s del cliente. El APPLY ya NO va por acá:
// corre en background (Celery) y el frontend hace polling del estado.
const REREAD_TIMEOUT_MS = 120_000;

// F4: el confirm corre el import SÍNCRONO (inline) y puede tardar minutos en
// archivos grandes sobre Neon. El timeout global de 15s cortaría al usuario con
// un error aunque el backend siga importando (y un reintento daría 409). Se le da
// margen POR ENCIMA del TTL del lease del backend (15 min) para que un import
// legítimamente largo termine; si aun así vence, el caller lo trata como
// "sigue en curso" (no como error) y refresca el estado desde la lista.
const CONFIRM_TIMEOUT_MS = 16 * 60_000; // 16 min > TTL del lease (15 min)

export interface UploadedFileItem {
  id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  purpose: string;
  processing_status: string;
  created_at: string;
}

export interface ColumnAtRisk {
  column: string;
  null_pct: number;
  recommendation: string;
}

// F8c: diagnóstico de riesgo por columna dentro de un contexto (reemplaza el
// legacy ColumnAtRisk global). null_ratio es 0.0–1.0 → la UI lo muestra ×100.
export interface ContextualColumnRisk {
  context_id: string;
  entity_type: string;
  source_column: string;
  target_field: string;
  null_ratio: number; // 0.0–1.0
  affected_rows: number;
  null_rows: number;
  invalid_rows: number;
  field_requirement: "required" | "explicitly_selected" | "optional";
  mapping_source: "tenant_history" | "heuristic" | "fuzzy" | "llm" | "none";
  user_selected: boolean;
  allowed_actions: string[]; // "drop_column" | "route_affected_rows_to_others"
  recommendation: string;
}

// F8c: decisión del usuario sobre una columna riesgosa dentro de un contexto.
export interface ColumnRiskDecision {
  context_id: string;
  source_column: string;
  target_field: string;
  action: "drop_column" | "route_affected_rows_to_others";
}

// F7d: fila de muestra del preview de un maestro (cliente/proveedor). El
// backend minimiza PII a propósito — nunca trae DNI/CUIT/email/teléfono
// crudos, solo nombre + estado + un diagnóstico corto.
export interface MasterPreviewSample {
  row_index: number;
  status: "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review";
  display_name: string | null;
  existing_name: string | null;
  issue: string | null;
}

// F7d: preview de una hoja de maestro — cuántas filas son create/update/
// needs_review/invalid/duplicate ANTES de confirmar. Solo diagnóstico, no
// persiste nada; el confirm real solo importa create/update.
export interface MasterPreviewSummary {
  context_id: string | null;
  entity_type: "customer" | "supplier";
  to_create: number;
  to_update: number;
  needs_review: number;
  invalid: number;
  duplicates: number;
  samples: MasterPreviewSample[];
}

export interface FilePreview {
  file_id: string;
  processing_status: string;
  parsed_summary_json: Record<string, unknown> | null;
  columns_at_risk: ColumnAtRisk[];
  // F8c: diagnóstico contextual por columna; opcional para archivos viejos que
  // no lo hayan traído en su parsed_summary_json.
  contextual_column_risk?: ContextualColumnRisk[];
  // F7e: vacío si el archivo no tiene hojas de maestro o no se pudo estimar el mapeo.
  master_previews: MasterPreviewSummary[];
}

/**
 * F-H3: qué le PASARÍA al stock de un producto si se aplicara la historia del
 * archivo. Nada de esto se aplicó — el import calcula y reporta; aplicar la
 * historia es una decisión aparte, hoja por hoja.
 */
export interface InventoryImpactItem {
  product_id: string;
  product_name: string;
  /** Saldo ANTES del archivo (o el absoluto que declara un catálogo). */
  saldo_inicial: number;
  /** Saldo tras reproducir compras y ventas por fecha. */
  saldo_final: number;
  compradas: number;
  vendidas: number;
  /** Menor saldo alcanzado durante la secuencia, y cuándo (ISO). */
  minimo: number;
  minimo_en?: string | null;
  /**
   * Primer día en que el saldo se fue abajo de cero. `null` = nunca.
   * Tocar negativo NO es lo mismo que quedar negativo (`saldo_final < 0`): un
   * final sano con un pozo en el medio significa que faltan compras viejas.
   */
  primer_negativo_en?: string | null;
}

/**
 * F-H3.d.4 — una venta cuyo descuento no se pudo aplicar por falta de stock.
 *
 * NO se anula: la venta ya está en los libros y anularla cambiaría facturación
 * confirmada. Queda pendiente hasta que el usuario cargue el inventario que falta.
 */
export interface PendingSaleItem {
  sale_id: string;
  product_id: string;
  product_name: string;
  quantity: number;
  /** Unidades que había cuando le tocó el turno. Siempre menor que `quantity`. */
  disponible: number;
}

/**
 * F-H3.d.4 — resultado de aplicar la historia de ventas del archivo al inventario.
 *
 * Los números son los RECALCULADOS en esa corrida, no los que devolvió el confirm:
 * entre confirmar y aplicar el stock pudo cambiar.
 */
export interface InventoryReplayResult {
  file_id: string;
  dry_run: boolean;
  aplicadas: number;
  /** Ya estaban descontadas (aplicar de nuevo, o descontadas en vivo). No es error. */
  ya_aplicadas: number;
  sin_stock: PendingSaleItem[];
  impacto: InventoryImpactItem[];
  hojas: string[];
  /** `false` = alguna venta no tiene registrada su hoja; el alcance fue el archivo. */
  alcance_por_hoja: boolean;
  warnings: string[];
}

/**
 * F-H3.e — qué le hace al INVENTARIO cada hoja, y entre qué puede elegir el
 * usuario. Lo calcula el backend a partir del mapeo borrador: el default y las
 * opciones dependen de la entidad de la hoja y de los campos que el mapeo cubre
 * (sin `cantidad` mapeada, la hoja no mueve unidades). Una tabla fija acá sería
 * una copia de una regla de dominio — el defecto que rompió el mapeo de columnas.
 */
export interface InventoryEffectOption {
  value: string;
  /** Describe QUÉ le pasa al stock, no el nombre técnico del modo. */
  label: string;
}

export interface SheetInventoryEffect {
  context_id: string;
  /** Nombre legible de la hoja, nunca el `context_id` crudo. */
  label: string;
  default: string;
  /** Siempre incluye `default`. Con un solo elemento no hay nada que elegir. */
  options: InventoryEffectOption[];
}

export interface ConfirmIngestionResult {
  file_id: string;
  status: string;
  message: string;
  // Avisos human-in-the-loop tras confirmar (compras sin proveedor/producto, filas a
  // "Otros"). No bloquean; se muestran en un banner para que el usuario los revise.
  warnings?: string[];
  // F-H3.c: impacto proyectado sobre el inventario, para mostrarlo. Ordenado con
  // los productos que se van a negativo primero, y ACOTADO: comparar contra
  // `inventory_impact_total` antes de decir "estos son todos".
  inventory_impact?: InventoryImpactItem[];
  inventory_impact_total?: number;
}

/**
 * Tratamiento del stock de un archivo de catálogo/lista al confirmar:
 * - "opening_balance": saldo de apertura (ya lo tenía) → solo carga inventario,
 *   sin gasto de mercadería ni salida de caja.
 * - "purchase": compra → registra el gasto (COGS) y la baja de caja.
 */
export type StockTreatment = "opening_balance" | "purchase";

// Respuesta del upload: incluye un aviso (warning) opcional — p. ej. re-subida
// por nombre (versión actualizada) — y el id del archivo original si es duplicado.
export interface UploadResult {
  file_id: string;
  status: string;
  duplicate_of?: string | null;
  warning?: string | null;
}

/**
 * Un campo canónico al que se puede mapear una columna. Llega del backend: el
 * frontend NO mantiene su propia lista.
 *
 * Antes había una copia manual de `CANONICAL_FIELDS` comentada "mantener en
 * sync", y divergió — a `expense` le faltaban `payment_method` e `is_recurring`.
 * Como el `<select>` solo renderiza opciones de esa copia, un target sugerido
 * que no estuviera en ella hacía que el DOM cayera a la primera opción: la
 * pantalla decía "Sin mapear" mientras el estado mandaba `payment_method`.
 */
export interface FieldCatalogEntry {
  value: string;
  label: string;
  /** Solo UNA columna puede apuntarle: dos no se pueden desempatar sin inventar. */
  single_value: boolean;
}

export interface EntityFieldCatalog {
  /** Un `custom_field:` NO cubre un requerido (misma regla que el confirm). */
  required: string[];
  /**
   * Qué otro conjunto COMPLETO de campos cubre un requerido (F-H4):
   * `{ amount: ["unit_price", "quantity"] }` — si la planilla trae el precio
   * unitario y la cantidad, el total es una cuenta y no hace falta la columna.
   *
   * Viene del backend por la misma razón que `fields`: con una copia acá, la
   * pantalla bloquearía un archivo que el confirm acepta.
   */
  required_alternatives: Record<string, string[]>;
  fields: FieldCatalogEntry[];
}

export type FieldCatalog = Record<string, EntityFieldCatalog>;

export interface ColumnMappingSuggestion {
  source_column: string;
  normalized_column: string;
  sample_values: string[];
  target_field: string | null;
  confidence: number;
  // FASE 2 (A2): "llm" = la 4ª capa LLM desambiguó esta columna.
  source: "tenant_history" | "heuristic" | "fuzzy" | "llm" | "none";
  status: "mapped" | "unmapped" | "required_missing";
  context_id?: string | null;
}

export interface ColumnMapping {
  source_column: string;
  target_field: string; // campo canónico, "ignore", o "custom_field:{key}"
  context_id?: string | null;
  entity_type?: string | null;
  // F8c: el frontend lo marca en true solo cuando el usuario cambió el target
  // manualmente. El backend nunca lo infiere — es distinto de `source`/
  // `mapping_source` (que indican de dónde salió la sugerencia).
  user_selected?: boolean;
}

/** Contexto de mapeo: una hoja/tabla/grupo detectado dentro de un archivo. */
export interface MappingContext {
  context_id: string;
  label: string;
  source_kind: "sheet" | "table" | "text_group" | "ocr_group";
  entity_type: "sale" | "expense" | "product" | "customer" | "supplier" | null;
  headers: string[] | null;
  fields: string[] | null;
  preview_rows: Record<string, unknown>[];
  row_count: number;
}

/** Qué se lleva puesto el borrado de un archivo (espejo de FileDeletionPreviewResponse). */
export interface FileDeletionPreview {
  file_id: string;
  ventas: number;
  gastos: number;
  productos: number;
  movimientos_stock: number;
  otros: number;
  /** Filas de "Otros" que el usuario ya clasificó: NO se borran. */
  otros_ya_clasificados: number;
  /** Hay registros de este archivo editados a mano — el borrado los revierte igual. */
  has_user_edits: boolean;
  /** Archivo importado antes del ledger: sus productos no se pueden rastrear. */
  productos_no_rastreables: boolean;
  /** Productos que el archivo MODIFICÓ y vuelven a su valor anterior. */
  productos_a_restaurar: number;
  /** Lo que NO se va a poder revertir, con nombre y motivo. */
  conservados: PreservedEntity[];
}

/**
 * Una entidad que sobrevive al borrado, y por qué. `fields` sólo viene cuando la
 * decisión es por campo: dice cuáles no se restauran mientras el resto sí.
 */
export interface PreservedEntity {
  entity_type: "product" | "customer" | "supplier" | "sale" | "expense";
  id: string;
  name: string;
  reasons: string[];
  fields: string[];
}

/**
 * Resultado del borrado. El endpoint dejó de responder 204 mudo: la UI necesita
 * distinguir "se eliminó todo" de "se eliminó, pero quedaron N cosas".
 */
export interface FileDeletionResult {
  status: "deleted";
  fully_reverted: boolean;
  deleted: Record<string, number>;
  restored: Record<string, number>;
  conservados: PreservedEntity[];
}

export interface TenantColumnMapping {
  id: string;
  entity_type: string;
  source_column: string;
  target_field: string;
  confirmed_count: number;
  last_seen_at: string;
}

// ── Relectura de archivos (REREAD_FILE) ──────────────────────────────────────
// Espejo de los schemas backend en app/schemas/ingestion.py.

export interface RereadCounts {
  to_update: number;
  preserved: number;
  new: number;
  to_void: number;
  unchanged: number;
  products_new: number;
  products_restock: number;
}

export interface RereadItem {
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface RereadPreviewResponse {
  file_id: string;
  counts: RereadCounts;
  legacy_fallback: boolean;
  sample_changes: RereadItem[];
}

export interface RereadApplyResponse {
  file_id: string;
  run_id: string;
  to_update: number;
  preserved: number;
  new: number;
  voided: number;
  inserted: number;
  legacy_fallback: boolean;
  items: RereadItem[];
}

// El apply corre en background; el POST devuelve el run para hacer polling.
export interface RereadApplyStartResponse {
  file_id: string;
  run_id: string;
  status: string; // "RUNNING"
}

export interface RereadRunStatusResponse {
  run_id: string;
  file_id: string;
  status: string; // RUNNING | APPLIED | FAILED
  to_update: number;
  preserved: number;
  new: number;
  voided: number;
  inserted: number;
  legacy_fallback: boolean;
  items: RereadItem[];
  error: string | null;
}

// F9b (Task 7 backend / Task 8 frontend): clientes/proveedores/productos que
// la relectura tocó pero el undo NO restauró porque alguien los editó después
// (política touched-since — nunca se pisa una edición manual en silencio).
// `kind` es "customer" | "supplier" | "product"; `reason` siempre
// "edited_after_reread" por ahora, pero se deja como string abierto por si el
// backend agrega motivos nuevos sin romper el frontend.
export interface RereadNotRevertedEntity {
  kind: string;
  id: string;
  reason: string;
}

export interface RereadUndoResponse {
  run_id: string;
  restored: number;
  removed: number;
  status: string;
  not_reverted_entities: RereadNotRevertedEntity[];
}

export const ingestionService = {
  async upload(
    file: File,
    fileHint: string = "general",
    onProgress?: (percent: number) => void,
    // Override explícito: reimportar un archivo cuyo contenido EXACTO ya fue
    // importado (el backend, por defecto, lo bloquea con 409 para no duplicar).
    allowDuplicate: boolean = false,
  ): Promise<UploadResult> {
    const fd = new FormData();
    fd.append("file", file);
    const qs = new URLSearchParams({ file_hint: fileHint });
    if (allowDuplicate) qs.set("allow_duplicate", "true");
    const res = await api.post<UploadResult>(
      `/ingestion/upload?${qs.toString()}`,
      fd,
      {
        headers: { "Content-Type": "multipart/form-data" },
        onUploadProgress: onProgress
          ? (e) => {
              const total = e.total ?? 1;
              onProgress(Math.round((e.loaded / total) * 100));
            }
          : undefined,
      },
    );
    return res.data;
  },

  async listFiles(): Promise<UploadedFileItem[]> {
    const res = await api.get<UploadedFileItem[]>("/ingestion/files");
    return res.data;
  },

  /**
   * Un archivo puntual por id. `null` cuando el backend responde 404, que es la
   * única evidencia de que no existe o fue eliminado.
   *
   * `listFiles` pagina de a 50 ordenando por fecha descendente, así que no
   * alcanza para resolver un link a un archivo viejo: no aparecer en esa
   * página no prueba nada sobre el archivo.
   */
  async getFile(fileId: string): Promise<UploadedFileItem | null> {
    try {
      const res = await api.get<UploadedFileItem>(`/ingestion/files/${fileId}`);
      return res.data;
    } catch (err) {
      const axiosErr = err as AxiosError;
      if (axiosErr.response?.status === 404) return null;
      throw err; // 500/red: no se pudo preguntar ≠ no existe
    }
  },

  /**
   * Returns null when the file is still PENDING/PROCESSING (backend returns 409).
   * Throws on other errors (404, 500, etc.).
   */
  async getPreview(fileId: string): Promise<FilePreview | null> {
    try {
      const res = await api.get<FilePreview>(
        `/ingestion/files/${fileId}/preview`,
      );
      return res.data;
    } catch (err) {
      const axiosErr = err as AxiosError;
      if (axiosErr.response?.status === 409) return null;
      throw err;
    }
  },

  /**
   * Campos canónicos, requeridos y escalares por entidad. Estático por deploy:
   * el panel lo pide una vez con `staleTime: Infinity`.
   */
  async getFieldCatalog(): Promise<FieldCatalog> {
    const res = await api.get<FieldCatalog>("/ingestion/field-catalog");
    return res.data;
  },

  async getColumnMappings(
    fileId: string,
    entityType: string = "sale",
    contextId?: string,
  ): Promise<ColumnMappingSuggestion[]> {
    const params = new URLSearchParams({ entity_type: entityType });
    if (contextId) params.set("context_id", contextId);
    const res = await api.get<ColumnMappingSuggestion[]>(
      `/ingestion/files/${fileId}/column-mappings?${params.toString()}`,
    );
    return res.data;
  },

  async getLearnedMappings(): Promise<TenantColumnMapping[]> {
    const res = await api.get<TenantColumnMapping[]>("/ingestion/column-mappings");
    return res.data;
  },

  async deleteLearnedMapping(mappingId: string): Promise<void> {
    await api.delete(`/ingestion/column-mappings/${mappingId}`);
  },

  async confirmFile(
    fileId: string,
    confirmedFields: Record<string, boolean>,
    columnMappings?: ColumnMapping[],
    contextConfirmed?: Record<string, boolean>,
    contextEntity?: Record<string, string>,
    // Origen del stock de cada hoja de productos: `{context_id: tratamiento}`.
    // Un string plano sigue valiendo para todas las hojas (compatibilidad).
    stockTreatment?: StockTreatment | Record<string, StockTreatment>,
    // F8c: decisiones del usuario sobre columnas riesgosas (drop / enrutar a Otros).
    columnRiskDecisions?: ColumnRiskDecision[],
    // F-H3.e: qué le hace al inventario cada hoja, `{context_id: modo}`. Sin esto
    // el backend aplica el default de cada hoja y `historical_replay` —el único
    // modo que escribe stock— era inalcanzable desde la pantalla.
    inventoryEffect?: Record<string, string>,
  ): Promise<ConfirmIngestionResult> {
    const res = await api.post<ConfirmIngestionResult>(
      `/ingestion/files/${fileId}/confirm`,
      {
        confirmed_fields: confirmedFields,
        column_mappings: columnMappings ?? [],
        context_confirmed: contextConfirmed ?? {},
        context_entity: contextEntity ?? {},
        stock_treatment: stockTreatment ?? null,
        column_risk_decisions: columnRiskDecisions ?? [],
        // `null` y no `{}`: un dict vacío y "no mandé nada" significan lo mismo
        // para el backend (cada hoja toma su default), pero mandar null lo dice.
        inventory_effect: inventoryEffect ?? null,
      },
      { timeout: CONFIRM_TIMEOUT_MS },
    );
    return res.data;
  },

  /**
   * F-H3.e: qué propone Véktor para el inventario de cada hoja, con el mapeo
   * borrador. Read-only. Se vuelve a pedir cuando el mapeo cambia: mapear
   * `cantidad` es lo que habilita que una hoja pueda aplicar su historia.
   */
  async fetchInventoryEffects(
    fileId: string,
    body: {
      columnMappings: ColumnMapping[];
      contextEntity: Record<string, string>;
    },
    signal?: AbortSignal,
  ): Promise<SheetInventoryEffect[]> {
    const res = await api.post<SheetInventoryEffect[]>(
      `/ingestion/files/${fileId}/inventory-effects`,
      {
        column_mappings: body.columnMappings,
        context_entity: body.contextEntity,
      },
      { signal },
    );
    return res.data;
  },

  // F8c: recalcula el riesgo contextual de columnas en vivo (p. ej. tras
  // cambiar un mapeo o confirmar un contexto), sin persistir nada.
  async recomputeColumnRisk(
    fileId: string,
    body: {
      columnMappings: ColumnMapping[];
      contextEntity: Record<string, string>;
      confirmedFields: Record<string, boolean>;
      contextConfirmed: Record<string, boolean>;
    },
    signal?: AbortSignal,
  ): Promise<ContextualColumnRisk[]> {
    const res = await api.post<ContextualColumnRisk[]>(
      `/ingestion/files/${fileId}/column-risk`,
      {
        column_mappings: body.columnMappings,
        context_entity: body.contextEntity,
        confirmed_fields: body.confirmedFields,
        context_confirmed: body.contextConfirmed,
      },
      { signal },
    );
    return res.data;
  },

  async cancelFile(fileId: string): Promise<{ file_id: string; status: string }> {
    const res = await api.post<{ file_id: string; status: string }>(
      `/ingestion/files/${fileId}/cancel`,
    );
    return res.data;
  },

  /**
   * Qué datos se borran si se elimina este archivo. Read-only: alimenta la
   * advertencia previa. El borrado revierte TAMBIÉN lo editado a mano, así que
   * el usuario tiene que poder verlo antes de aceptar.
   */
  async getDeletionPreview(fileId: string): Promise<FileDeletionPreview> {
    const res = await api.get<FileDeletionPreview>(
      `/ingestion/files/${fileId}/deletion-preview`,
    );
    return res.data;
  },

  /**
   * Borra el archivo Y revierte lo que importó (ventas, gastos, stock, "Otros"
   * y los productos que creó). `confirm` es obligatorio: sin él el backend
   * responde 409 con el preview y no toca nada.
   */
  async deleteFile(fileId: string, confirm = false): Promise<FileDeletionResult> {
    const res = await api.delete<FileDeletionResult>(
      `/ingestion/files/${fileId}?confirm=${confirm}`,
    );
    return res.data;
  },

  async reprocessFile(fileId: string): Promise<void> {
    await api.post(`/ingestion/files/${fileId}/reprocess`);
  },

  // ── Relectura de archivos (REREAD_FILE) ──────────────────────────────────
  async rereadPreview(fileId: string): Promise<RereadPreviewResponse> {
    const res = await api.post<RereadPreviewResponse>(
      `/ingestion/files/${fileId}/reread/preview`,
      undefined,
      // Relectura sobre archivos grandes (miles de filas) puede tardar más que
      // el default de 15s del cliente; el edge de Railway corta a ~300s.
      { timeout: REREAD_TIMEOUT_MS },
    );
    return res.data;
  },

  // Encola el apply en background y devuelve el run para hacer polling.
  async rereadApply(fileId: string): Promise<RereadApplyStartResponse> {
    const res = await api.post<RereadApplyStartResponse>(
      `/ingestion/files/${fileId}/reread/apply`,
    );
    return res.data;
  },

  async rereadRunStatus(
    fileId: string,
    runId: string,
  ): Promise<RereadRunStatusResponse> {
    const res = await api.get<RereadRunStatusResponse>(
      `/ingestion/files/${fileId}/reread/runs/${runId}`,
    );
    return res.data;
  },

  /**
   * F-H3.d.4 — aplica al inventario la historia de ventas del archivo.
   *
   * `dryRun` corre el MISMO cálculo sin escribir. Sin `contextIds` aplica todas
   * las hojas del archivo. El descuento es idempotente: reintentarlo después de
   * cargar el stock que faltaba sólo aplica lo que había quedado pendiente.
   */
  async applyInventoryReplay(
    fileId: string,
    options?: { contextIds?: string[]; dryRun?: boolean },
  ): Promise<InventoryReplayResult> {
    const res = await api.post<InventoryReplayResult>(
      `/ingestion/files/${fileId}/inventory-replay`,
      {
        context_ids: options?.contextIds ?? null,
        dry_run: options?.dryRun ?? false,
      },
      { timeout: CONFIRM_TIMEOUT_MS },
    );
    return res.data;
  },

  async rereadUndo(fileId: string): Promise<RereadUndoResponse> {
    const res = await api.post<RereadUndoResponse>(
      `/ingestion/files/${fileId}/reread/undo`,
      undefined,
      { timeout: REREAD_TIMEOUT_MS },
    );
    return res.data;
  },
};
