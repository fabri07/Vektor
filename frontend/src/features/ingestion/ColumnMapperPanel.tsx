"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle,
  AlertCircle,
  AlertTriangle,
  XCircle,
  ArrowRight,
} from "lucide-react";
import {
  ingestionService,
  type ColumnMapping,
  type ColumnMappingSuggestion,
  type ColumnRiskDecision,
  type ConfirmIngestionResult,
  type ContextualColumnRisk,
  type FieldCatalogEntry,
  type InventoryImpactItem,
  type MappingContext,
  type MasterPreviewSummary,
  type StockTreatment,
  type SheetInventoryEffect,
  type ShippingAction,
  type PurchaseCostBase,
  type PurchaseCostDecision,
  type PurchaseLineShipping,
  type PurchaseSharedShipping,
  type SheetPurchaseGroups,
} from "@/services/ingestion.service";
import { Button } from "@/components/ui/Button";
import { InventoryImpactPanel } from "./InventoryImpactPanel";
import { StockTreatmentChoice, summaryHasStock } from "./stockTreatment";
import { InventoryEffectChoice } from "./InventoryEffectChoice";
import { ShippingDecisionChoice } from "./ShippingDecisionChoice";
import { PurchaseCostChoice } from "./PurchaseCostChoice";
import { PurchaseGroupsPreview } from "./PurchaseGroupsPreview";
import { useToastStore } from "@/stores/toastStore";
import { MasterPreviewPanel } from "./MasterPreviewPanel";
import { ColumnRiskDecisionsPanel } from "./ColumnRiskDecisionsPanel";
import {
  customFieldCollisions,
  explainMissing,
  missingRequiredFields,
  scalarCollisions,
  type SheetIssues,
} from "./mappingRules";

// F8c: identidad estable de una columna riesgosa dentro del archivo: (contexto,
// columna). Se serializa la tupla con JSON.stringify (sin separador propenso a
// colisión) — misma convención que el panel de decisiones. Se usa como clave del
// touched-set que marca `user_selected` SOLO en cambios manuales de mapeo.
const riskKey = (contextId: string, sourceColumn: string): string =>
  JSON.stringify([contextId, sourceColumn]);

// El catálogo de campos NO vive acá: lo sirve el backend
// (`GET /ingestion/field-catalog`), derivado de las MISMAS estructuras que usan
// la validación del confirm y el importador.
//
// Antes había una copia manual comentada "mantener en sync" y divergió: a
// `expense` le faltaban `payment_method` e `is_recurring`. Como el `<select>`
// solo renderiza opciones de esa copia, cuando el backend sugería
// `payment_method` ninguna `<option>` matcheaba, el DOM caía a la primera y la
// pantalla mostraba "Sin mapear" mientras el estado enviaba `payment_method`
// (incidente ASTERIA, 2026-07-31). Dos listas que deben coincidir a mano son
// dos listas que tarde o temprano no coinciden.

/**
 * Catálogo de campos del backend. Estático por deploy, así que una sola query
 * compartida por todas las secciones (react-query dedupea por `queryKey`).
 */
function useFieldCatalog() {
  return useQuery({
    queryKey: ["ingestion-field-catalog"],
    queryFn: () => ingestionService.getFieldCatalog(),
    staleTime: Infinity,
  });
}

const ENTITY_TYPE_LABELS: Record<string, string> = {
  sale: "Ventas",
  expense: "Gastos",
  product: "Productos",
  customer: "Clientes",
  supplier: "Proveedores",
};

// Secciones a las que se puede mandar una hoja de planilla.
//
// Clientes y Proveedores estuvieron AFUERA un tiempo porque
// `_import_master_entities` leía el `entity_type` del summary sin consultar el
// override, y buscaba las filas en `clientes_detectados`/`proveedores_detectados`
// — donde una hoja mal clasificada no las tiene. Elegir la sección confirmaba
// sin error y no importaba nada, así que era mejor no ofrecer la opción.
// Ahora el importador honra el override y resuelve las filas desde el bucket
// del tipo original, así que las cinco secciones son destinos reales.
const ENTITY_OPTIONS = ["sale", "expense", "product", "customer", "supplier"] as const;

// Las hojas que el parser SÍ clasificó como maestro siguen mostrando su sección
// (el camino de maestros funciona cuando la entidad viene del summary).
const ENTITY_OPTIONS_TEXTO = [
  "sale",
  "expense",
  "customer",
  "supplier",
] as const;

// Valor de "todavía no elegí". NO es "sale": el default silencioso a ventas es
// justo lo que hacía que una hoja que Véktor no supo clasificar (p. ej. un
// resumen derivado del Libro Diario) entrara como 1840 ventas sin que nadie lo
// decidiera.
const ENTITY_UNSET = "";

const SOURCE_LABELS: Record<string, string> = {
  tenant_history: "Historial",
  heuristic: "Heurística",
  fuzzy: "Similar",
  llm: "IA",
  none: "—",
};

// F4: un error de confirm que NO es de mapeo — 409 (confirm concurrente / archivo
// ya importado o importándose) o timeout del cliente (el import sigue en curso en
// el backend). No debe pintar el banner rojo de "revisá los campos".
function isTransientConfirmError(error: unknown): boolean {
  const e = error as { response?: { status?: number }; code?: string } | null;
  return e?.response?.status === 409 || e?.code === "ECONNABORTED";
}

/**
 * F-H6.d: el tenant no tiene habilitado el motor de costos de compra.
 *
 * `/purchase-groups` responde 403 y eso NO es un error que haya que mostrar: es
 * la compuerta de rollout, y la degradación correcta es que el tercer eje y la
 * vista previa del reparto simplemente no aparezcan. Lo que sí hay que evitar es
 * seguir preguntando: la clave de la consulta cambia con cada edición del mapeo,
 * así que sin este freno un tenant fuera de la lista se come un 403 por tecla.
 */
function esMotorDeCostosDeshabilitado(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === 403;
}

// Maneja el error transitorio del confirm: avisa amable y refresca la lista.
// Devuelve true si lo manejó (para no caer en el banner de error de mapeo).
function handleTransientConfirmError(
  error: unknown,
  toast: (msg: string, variant: "warning" | "info") => void,
  invalidate: () => void,
): boolean {
  const e = error as { response?: { status?: number }; code?: string } | null;
  if (e?.response?.status === 409) {
    toast(
      "Ya se está importando este archivo o ya se importó — actualizá la lista",
      "warning",
    );
    invalidate();
    return true;
  }
  if (e?.code === "ECONNABORTED") {
    // Timeout del cliente: el import sigue corriendo en el backend. No es un error.
    toast(
      "El import sigue en curso — revisá la lista en unos segundos",
      "info",
    );
    invalidate();
    return true;
  }
  return false;
}

// FASE 2 (A2)/A3: color de la confianza del mapeo (verde alto / ámbar medio / rojo bajo).
function confidenceColor(confidence: number): string {
  if (confidence >= 0.75) return "text-vk-success";
  if (confidence >= 0.5) return "text-vk-warning";
  return "text-vk-danger";
}

/**
 * F-M: por qué una columna no se mapeó sola, y entre qué elegir.
 *
 * Vive en UN solo lugar porque el panel renderiza columnas en tres caminos
 * distintos (multi-hoja, lista de revisión y tabla única) y este repo ya pagó
 * el precio de que dos de ellos divergieran.
 *
 * `duda` viaja también sin `options`: son los casos donde Véktor entendió el
 * encabezado y esta hoja no tiene campo donde ponerlo. Ahí no hay entre qué
 * elegir, pero explicarlo es la diferencia entre un hueco y un hueco con motivo.
 */
function AmbiguityHint({
  suggestion,
  fields,
  onPick,
}: {
  suggestion: ColumnMappingSuggestion;
  fields: FieldCatalogEntry[];
  onPick: (target: string) => void;
}) {
  if (!suggestion.duda) return null;
  const options = suggestion.options ?? [];
  const labelFor = (value: string) => fields.find((f) => f.value === value)?.label ?? value;
  return (
    <div className="rounded border border-vk-blue/30 bg-vk-blue/5 px-2 py-1.5">
      <p className="text-[11px] leading-snug text-vk-text-secondary">{suggestion.duda}</p>
      {options.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {options.map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => onPick(option)}
              className="rounded border border-vk-blue/40 bg-vk-bg-light px-2 py-0.5 text-[11px] text-vk-text-primary hover:border-vk-blue hover:bg-vk-blue/10"
            >
              {labelFor(option)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Propósitos posibles cuando el tipo del archivo quedó ambiguo ("general").
const PURPOSE_OPTIONS: Array<{ value: string; label: string; field: "ventas" | "gastos" | "productos" }> = [
  { value: "ventas", label: "Ventas", field: "ventas" },
  { value: "gastos", label: "Gastos", field: "gastos" },
  { value: "stock", label: "Productos / Stock", field: "productos" },
];

function StatusDot({ status }: { status: ColumnMappingSuggestion["status"] }) {
  if (status === "mapped") {
    return <span className="h-2 w-2 rounded-full bg-vk-success shrink-0" title="Mapeado" />;
  }
  // F-M: entender la columna y no poder decidir NO es lo mismo que no
  // entenderla. Si cayera al punto de "Sin mapear" de abajo, la pantalla
  // borraría justamente la distinción que el backend calcula.
  if (status === "ambiguo") {
    return (
      <span
        className="h-2 w-2 rounded-full bg-vk-blue shrink-0"
        title="Necesita que elijas entre dos lecturas"
      />
    );
  }
  if (status === "required_missing") {
    return (
      <span
        className="h-2 w-2 rounded-full bg-vk-danger shrink-0"
        title="Campo requerido faltante"
      />
    );
  }
  return <span className="h-2 w-2 rounded-full bg-vk-warning shrink-0" title="Sin mapear" />;
}

// Modal secuencial para columnas sin mapear
function UnmappedModal({
  column,
  entityType,
  onResolve,
  onClose,
}: {
  column: string;
  entityType: string;
  onResolve: (target: string) => void;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState("");
  const [customKey, setCustomKey] = useState("");
  const [mode, setMode] = useState<"field" | "custom" | "ignore">("field");

  const { data: catalog } = useFieldCatalog();
  const fields = catalog?.[entityType]?.fields ?? [];

  function handleConfirm() {
    if (mode === "field" && selected) {
      onResolve(selected);
    } else if (mode === "custom" && customKey.trim()) {
      onResolve(`custom_field:${customKey.trim().toLowerCase().replace(/\s+/g, "_")}`);
    } else if (mode === "ignore") {
      onResolve("ignore");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-xl">
        <div className="mb-4 flex items-start justify-between gap-2">
          <div>
            <p className="text-xs text-vk-text-muted">Columna sin mapear</p>
            <p className="mt-0.5 font-mono text-sm font-semibold text-vk-text-primary">
              {column}
            </p>
          </div>
          <AlertCircle className="h-5 w-5 shrink-0 text-vk-warning" />
        </div>

        <div className="mb-4 space-y-2">
          <button
            type="button"
            onClick={() => setMode("field")}
            className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
              mode === "field"
                ? "border-vk-blue bg-vk-info-bg text-vk-blue"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            Asignar a un campo de Véktor
          </button>
          {mode === "field" && (
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary focus:border-vk-blue focus:outline-none"
            >
              <option value="">Elegir campo...</option>
              {fields.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          )}

          <button
            type="button"
            onClick={() => setMode("custom")}
            className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
              mode === "custom"
                ? "border-vk-blue bg-vk-info-bg text-vk-blue"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            Guardar como campo personalizado
          </button>
          {mode === "custom" && (
            <input
              type="text"
              value={customKey}
              onChange={(e) => setCustomKey(e.target.value)}
              placeholder="nombre_del_campo (sin espacios)"
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-muted focus:border-vk-blue focus:outline-none"
            />
          )}

          <button
            type="button"
            onClick={() => setMode("ignore")}
            className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
              mode === "ignore"
                ? "border-vk-border-w bg-vk-bg-light text-vk-text-muted"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            Ignorar esta columna
          </button>
        </div>

        <div className="flex gap-2">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 rounded-lg border border-vk-border-w px-3 py-2 text-sm text-vk-text-secondary hover:bg-vk-bg-light transition-colors"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={
              (mode === "field" && !selected) ||
              (mode === "custom" && !customKey.trim())
            }
            className="flex-1 rounded-lg bg-vk-blue px-3 py-2 text-sm font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
          >
            Confirmar
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Reparte los avisos del parser entre las hojas que los originaron.
 *
 * Un aviso "pertenece" a una hoja si menciona su nombre — el parser los redacta
 * así ("La hoja 'Ganancias' parece derivada del Libro Diario…"). El que no
 * matchea con ninguna hoja NO se descarta: vuelve como `general` y se muestra a
 * nivel archivo. Perder un aviso sería peor que mostrarlo en el lugar genérico.
 */
/**
 * F-H3.c — el impacto sobre el inventario después de confirmar.
 *
 * Vive en un hook porque este archivo tiene DOS flujos de confirmación
 * (`MultiContextMapper` y `ColumnMapperPanel`) con el mismo `onSuccess`
 * duplicado. F-B va a unificarlos; hasta entonces, esto evita que la próxima
 * copia sea justo la que decide si el usuario ve o no lo que le pasaría al
 * stock.
 *
 * Cuando hay impacto el panel NO se cierra: una tabla de saldo inicial →
 * movimientos → saldo final no entra en un toast, y es exactamente lo que hay
 * que mirar para decidir si aplicar esa historia.
 */
function useImpactoPostConfirm(onDone: () => void, fileId: string) {
  const [impacto, setImpacto] = useState<InventoryImpactItem[]>([]);
  const [impactoTotal, setImpactoTotal] = useState(0);

  /** `true` si hay algo que mostrar (y por lo tanto NO hay que cerrar). */
  function registrar(result: ConfirmIngestionResult): boolean {
    const items = result.inventory_impact ?? [];
    if (items.length === 0) return false;
    setImpacto(items);
    setImpactoTotal(result.inventory_impact_total ?? items.length);
    return true;
  }

  const vista =
    impacto.length > 0 ? (
      <div className="ml-2 mt-3 rounded-xl border border-vk-border-w bg-vk-surface-w p-4">
        <p className="text-sm font-semibold text-vk-text-primary">
          Datos importados
        </p>
        <InventoryImpactPanel
          items={impacto}
          total={impactoTotal}
          fileId={fileId}
        />
        <div className="mt-3">
          <Button size="sm" onClick={onDone}>
            Listo
          </Button>
        </div>
      </div>
    ) : null;

  return { registrar, vista };
}

export function splitWarningsByContext(
  warnings: string[],
  contexts: MappingContext[],
): { byContext: Record<string, string[]>; general: string[] } {
  const byContext: Record<string, string[]> = {};
  const general: string[] = [];
  for (const w of warnings) {
    const texto = w.toLowerCase();
    const hojas = contexts.filter((c) => {
      // trim: hay labels con espacio al final ("precios y stock ").
      const label = (c.label ?? "").trim().toLowerCase();
      // >= 3 evita que un label cortísimo matchee cualquier cosa.
      return label.length >= 3 && texto.includes(label);
    });
    if (hojas.length === 0) {
      general.push(w);
      continue;
    }
    for (const c of hojas) (byContext[c.context_id] ??= []).push(w);
  }
  return { byContext, general };
}

// Status efectivo de una columna: si hay target local → mapped; "ignore" → unmapped;
// si no hay target → status del backend (captura required_missing).
function effectiveStatus(
  s: ColumnMappingSuggestion,
  target: string,
): ColumnMappingSuggestion["status"] {
  if (target === "ignore") return "unmapped";
  if (target) return "mapped";
  return s.status;
}

/**
 * F-A: ¿el target que el usuario eligió a mano sigue teniendo sentido en la
 * entidad nueva?
 *
 * «Sin mapear», «Ignorar» y los campos personalizados NO pertenecen a ningún
 * schema, así que sobreviven a cualquier cambio de sección. Un campo canónico
 * sobrevive sólo si el catálogo de la entidad nueva lo tiene: conservar
 * `amount` en una hoja de Productos dejaría el `<select>` mostrando «(campo
 * desconocido)» — exactamente el síntoma que cerró el catálogo de fuente única.
 * Cuando no sigue siendo elegible, la columna cae a la sugerencia nueva; no se
 * inventa ningún reemplazo.
 */
function targetSobreviveALaEntidad(
  target: string,
  fields: FieldCatalogEntry[],
): boolean {
  if (!target || target === "ignore" || target.startsWith("custom_field:")) {
    return true;
  }
  return fields.some((f) => f.value === target);
}

// ── Mapeo multi-contexto (multi-hoja): una sección por hoja/grupo ──────────────

function SheetMapperSection({
  fileId,
  context,
  included,
  entity,
  warnings,
  onIncludeChange,
  onMappingsChange,
  onEntityChange,
  onColumnTouched,
  isColumnTouched,
  onIssuesChange,
  stockTreatment,
  onStockTreatmentChange,
  inventoryEffect,
  effectValue,
  onEffectChange,
  shippingValue,
  onShippingChange,
  costoValue,
  onCostoChange,
  grupos,
  resaltada,
}: {
  fileId: string;
  context: MappingContext;
  included: boolean;
  entity: string;
  // Avisos que el parser generó para ESTA hoja (ver `warningsForContext`).
  warnings: string[];
  onIncludeChange: (ctxId: string, included: boolean) => void;
  onMappingsChange: (ctxId: string, mappings: Record<string, string>) => void;
  onEntityChange: (ctxId: string, entity: string) => void;
  // F8c: se llama SOLO en cambios manuales de mapeo (marca user_selected).
  onColumnTouched?: (ctxId: string, col: string) => void;
  // F-A: lectura del mismo touched-set que escribe `onColumnTouched`. Sin él la
  // sección no puede distinguir lo que eligió el usuario de lo que quedó de una
  // sugerencia, y al cambiar de sección pisaría las dos cosas por igual.
  isColumnTouched?: (ctxId: string, col: string) => boolean;
  // Problemas que impiden importar ESTA hoja. El padre los junta para decidir si
  // habilita Confirmar — así el bloqueo usa las mismas reglas que el confirm del
  // backend en vez de descubrirse recién con un 422.
  onIssuesChange?: (ctxId: string, issues: SheetIssues) => void;
  // Origen del stock de ESTA hoja (solo se pregunta si es de productos).
  stockTreatment: StockTreatment;
  onStockTreatmentChange: (ctxId: string, value: StockTreatment) => void;
  // F-H3.e: qué le hace al inventario ESTA hoja. Lo sirve el backend con el
  // mapeo borrador; `undefined` mientras la consulta está en vuelo.
  inventoryEffect?: SheetInventoryEffect;
  effectValue: string;
  onEffectChange: (ctxId: string, value: string) => void;
  // F-H6.b: decisión sobre los envíos sin comprobante de ESTA hoja.
  // `null` = todavía no eligió → no se registran.
  shippingValue: ShippingAction | null;
  onShippingChange: (ctxId: string, value: ShippingAction | null) => void;
  costoValue: {
    base: PurchaseCostBase;
    shared: PurchaseSharedShipping;
    line: PurchaseLineShipping;
  };
  onCostoChange: (
    ctxId: string,
    patch: {
      base?: PurchaseCostBase;
      shared?: PurchaseSharedShipping;
      line?: PurchaseLineShipping;
    },
  ) => void;
  // F-H6.d: cómo agrupa el servidor las líneas de ESTA hoja por comprobante y
  // cómo repartiría el envío. `undefined` mientras la consulta está en vuelo o
  // cuando la hoja no declara envío compartido.
  grupos?: SheetPurchaseGroups;
  // El banner de faltantes mandó acá pero no pudo señalar una columna: se marca
  // la tarjeta para que la persona sepa dónde aterrizó.
  resaltada?: boolean;
}) {
  // Texto/imagen no tiene columnas: se mapea el grupo a un tipo, sin dropdowns.
  const isText = context.headers == null;
  const entityChosen = entity !== ENTITY_UNSET;
  const [mappings, setMappings] = useState<Record<string, string>>({});
  // Sección para la que ya se inicializó el mapeo (null = todavía ninguna).
  const [initializedFor, setInitializedFor] = useState<string | null>(null);
  // Columna que está creando un custom field + su key en edición.
  const [customFor, setCustomFor] = useState<string | null>(null);
  const [customKey, setCustomKey] = useState("");

  function selectTarget(col: string, value: string) {
    if (value === "__custom__") {
      setCustomFor(col);
      setCustomKey("");
      return;
    }
    setCustomFor((c) => (c === col ? null : c));
    setMappings((p) => ({ ...p, [col]: value }));
    // Cambio MANUAL del mapeo → marcar la columna como tocada por el usuario.
    onColumnTouched?.(context.context_id, col);
  }

  function commitCustom(col: string) {
    const key = customKey.trim().toLowerCase().replace(/\s+/g, "_");
    if (key) {
      setMappings((p) => ({ ...p, [col]: `custom_field:${key}` }));
      onColumnTouched?.(context.context_id, col);
    }
    setCustomFor(null);
    setCustomKey("");
  }

  const { data: suggestions = [], isLoading } = useQuery({
    queryKey: ["column-mappings", fileId, context.context_id, entity],
    queryFn: () =>
      ingestionService.getColumnMappings(fileId, entity, context.context_id),
    // Sin sección elegida no hay contra qué mapear: pedir sugerencias mandaría
    // `entity_type=""` y el backend lo rebota con 422.
    enabled: included && !isText && entityChosen,
  });

  // El catálogo se lee ANTES de inicializar el mapeo: la inicialización necesita
  // saber qué campos existen en la entidad nueva para no conservar un target que
  // allá no existe (ver `targetSobreviveALaEntidad`).
  const { data: catalog, isLoading: loadingCatalog } = useFieldCatalog();
  const fields = catalog?.[entity]?.fields ?? [];

  // Inicializar mapeos desde sugerencias, una vez POR SECCIÓN: si el usuario
  // reasigna la hoja (p. ej. de Ventas a Productos), las sugerencias vienen de
  // otro schema. Un flag booleano dejaba pegado el mapeo del schema anterior.
  //
  // F-A: es un MERGE, no un reemplazo. Reemplazar todo borraba las columnas que
  // la persona había mapeado a mano —veinte columnas de trabajo perdidas por
  // corregir la sección de la hoja—, así que lo tocado manda sobre la sugerencia
  // nueva y lo no tocado se recalcula, que es lo correcto: viene de otro schema.
  useEffect(() => {
    if (suggestions.length === 0 || initializedFor === entity) return;
    // Sin catálogo no se puede afirmar que un target tocado ya no es elegible, y
    // asumirlo lo descartaría por no encontrarlo. Este efecto vuelve a correr
    // cuando el catálogo llega.
    if (loadingCatalog) return;
    setMappings((prev) => {
      const next: Record<string, string> = {};
      for (const s of suggestions) {
        const col = s.source_column;
        const elegido = prev[col] ?? "";
        if (
          isColumnTouched?.(context.context_id, col) &&
          targetSobreviveALaEntidad(elegido, fields)
        ) {
          // Incluye el caso de un «Sin mapear» explícito: si la persona sacó esa
          // columna a propósito, cambiar de sección no es motivo para volver a
          // meterla.
          if (elegido) next[col] = elegido;
          continue;
        }
        if (s.target_field) next[col] = s.target_field;
      }
      return next;
    });
    setInitializedFor(entity);
    // `fields` es un array nuevo en cada render: se depende de `catalog`, que sí
    // es estable (react-query), y de `loadingCatalog` para reintentar al llegar.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    suggestions,
    initializedFor,
    entity,
    catalog,
    loadingCatalog,
    context.context_id,
    isColumnTouched,
  ]);

  // Reportar mapeos al padre cuando cambian (vacío en texto/imagen).
  useEffect(() => {
    onMappingsChange(context.context_id, mappings);
  }, [mappings, context.context_id, onMappingsChange]);
  // Requeridos REALES de la entidad: un requerido está cubierto solo si alguna
  // columna lo mapea a su campo canónico. Antes esto miraba el `status` que
  // mandó el backend por columna, así que mover la columna del nombre a un
  // campo personalizado dejaba el requerido descubierto sin que la UI se
  // enterara — daba el OK y el confirm respondía 422 (incidente ASTERIA).
  const faltanRequeridos = missingRequiredFields(
    catalog?.[entity]?.required ?? [],
    mappings,
    catalog?.[entity]?.required_alternatives ?? {},
  );
  // Las dos ramas del mapeo colisionan igual: un escalar canónico y un campo
  // propio guardan un valor por fila, así que de dos columnas al mismo destino
  // sobrevive una sola. Se muestran juntas porque para el usuario son el mismo
  // problema y la salida es la misma.
  const colisiones = [
    ...scalarCollisions(fields, mappings),
    ...customFieldCollisions(mappings),
  ];
  const reqMissing = faltanRequeridos.length > 0;

  // `faltanRequeridos`/`colisiones` se recomputan en cada render (arrays nuevos),
  // así que el efecto se dispara por su CONTENIDO y no por su identidad — si no,
  // reportaría en loop.
  const issuesKey = JSON.stringify([faltanRequeridos, colisiones]);

  // Reportar los problemas de ESTA hoja al padre (bloqueo de Confirmar).
  useEffect(() => {
    if (loadingCatalog) return; // sin catálogo no se puede afirmar que falte algo
    onIssuesChange?.(context.context_id, {
      missingRequired: faltanRequeridos,
      collisions: colisiones,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context.context_id, loadingCatalog, issuesKey, onIssuesChange]);
  // Columnas que no se mapearon ni se ignoraron a propósito: son las que se
  // perderían sin que nadie lo note. "ignore" NO cuenta — es una decisión.
  const unassigned = suggestions
    .filter((s) => !(mappings[s.source_column] ?? ""))
    .map((s) => s.source_column);
  const previewLines = context.preview_rows
    .map((r) => String(r.linea ?? Object.values(r)[0] ?? ""))
    .filter(Boolean)
    .slice(0, 6);

  return (
    <div
      // A dónde salta el banner de faltantes cuando NO puede señalar una columna
      // concreta. Por atributo y no por `id`: el `context_id` es texto libre.
      data-sheet-card={context.context_id}
      className={`rounded-lg border bg-vk-surface-w ${
        included ? "border-vk-border-w" : "border-vk-border-w/40 opacity-60"
      } ${resaltada ? "ring-2 ring-vk-danger/60" : ""}`}
    >
      <div className="flex items-center justify-between gap-3 border-b border-vk-border-w bg-vk-bg-light px-3 py-2">
        <label className="flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={included}
            onChange={(e) => onIncludeChange(context.context_id, e.target.checked)}
            className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
          />
          <span className="text-sm font-semibold text-vk-text-primary">{context.label}</span>
        </label>
        {/*
          La sección SIEMPRE se elige acá, aunque el parser haya clasificado la
          hoja. El reconocimiento automático sigue vigente —llega precargado como
          sugerencia—, pero es una sugerencia: en un archivo real mandó a
          "Productos" una hoja llamada Ventas de 1187 filas y otra llamada
          Clientes. Mientras esto era una chapita de sólo lectura, esa
          equivocación no tenía arreglo desde la UI.
        */}
        <div className="flex items-center gap-2">
          <select
            aria-label={`Sección de la hoja ${context.label}`}
            value={entity}
            onChange={(e) => onEntityChange(context.context_id, e.target.value)}
            className={`rounded border bg-vk-bg-light px-2 py-0.5 text-xs focus:border-vk-blue focus:outline-none ${
              entityChosen
                ? "border-vk-border-w text-vk-text-primary"
                : "border-vk-warning/50 text-vk-warning"
            }`}
          >
            <option value={ENTITY_UNSET}>Elegí qué es esta hoja…</option>
            {(isText ? ENTITY_OPTIONS_TEXTO : ENTITY_OPTIONS).map((value) => (
              <option key={value} value={value}>
                {ENTITY_TYPE_LABELS[value]}
              </option>
            ))}
          </select>
          <span className="text-[10px] font-medium text-vk-text-muted">
            {context.row_count} fila{context.row_count !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {/* Lo que el parser detectó sobre ESTA hoja. Antes vivía en un bloque
          general del archivo, lejos de la hoja que lo generó: el aviso de que
          una hoja es un resumen derivado del Libro Diario no llegaba a la
          decisión de incluirla o no. */}
      {warnings.length > 0 && (
        <div className="border-b border-vk-warning/20 bg-vk-warning-bg/40 px-3 py-2">
          {warnings.map((w) => (
            <p key={w} className="flex gap-1.5 text-[11px] leading-snug text-vk-warning">
              <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
              <span>{w}</span>
            </p>
          ))}
        </div>
      )}

      {included && !entityChosen && (
        <div className="px-3 py-3 text-xs text-vk-text-muted">
          Véktor no pudo determinar qué contiene esta hoja. Elegí a qué sección va
          para poder mapear sus columnas.
        </div>
      )}

      {included &&
        entityChosen &&
        (isText ? (
          <div className="p-2">
            <p className="mb-1 px-1 text-[10px] uppercase tracking-wide text-vk-text-muted">
              {context.row_count} línea{context.row_count !== 1 ? "s" : ""} detectada
              {context.row_count !== 1 ? "s" : ""}
            </p>
            <div className="space-y-0.5">
              {previewLines.map((line, i) => (
                <p
                  key={i}
                  className="truncate rounded bg-vk-bg-light/60 px-2 py-1 text-[11px] text-vk-text-secondary"
                  title={line}
                >
                  {line}
                </p>
              ))}
            </div>
          </div>
        ) : isLoading ? (
          <div className="px-3 py-3 text-xs text-vk-text-muted">Analizando columnas...</div>
        ) : (
          <div className="p-2">
            {reqMissing && (
              <div className="mb-2 flex items-center gap-2 rounded border border-vk-danger/30 bg-vk-danger-bg px-2 py-1 text-[11px] text-vk-danger">
                <XCircle className="h-3 w-3 shrink-0" />
                Campos obligatorios sin mapear (en rojo).
              </div>
            )}
            {/* Las columnas sin mapear se descartaban en silencio al confirmar
                (el confirm saltea los targets vacíos), así que el usuario no se
                enteraba de qué datos de su archivo NO entraban. Ahora se
                nombran: se les puede dar un nombre propio o ignorarlas a
                propósito, pero la decisión es visible. */}
            {unassigned.length > 0 && (
              <div className="mb-2 rounded border border-vk-warning/30 bg-vk-warning-bg/50 px-2 py-1.5 text-[11px] text-vk-warning">
                <p className="flex gap-1.5">
                  <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
                  <span>
                    {unassigned.length} columna{unassigned.length !== 1 ? "s" : ""} sin
                    asignar — no se {unassigned.length !== 1 ? "van" : "va"} a importar.
                    Ponele un nombre con «+ Campo personalizado» o marcala «Ignorar».
                  </span>
                </p>
                <p className="mt-1 truncate pl-4.5 font-mono text-[10px] opacity-80">
                  {unassigned.join(", ")}
                </p>
              </div>
            )}
            {/* Requerido descubierto: la salida concreta, nombrando el campo.
                Antes esto solo se sabía al confirmar, con un 422 que decía
                «Campos requeridos sin mapear: name» y nada más. */}
            {faltanRequeridos.length > 0 && (
              <div className="mb-2 rounded border border-vk-danger/40 bg-vk-danger/5 px-2 py-1.5 text-[11px] text-vk-danger">
                <p className="flex gap-1.5">
                  <XCircle className="mt-px h-3 w-3 shrink-0" />
                  <span>
                    Falta un dato obligatorio de esta hoja:{" "}
                    <strong>
                      {faltanRequeridos
                        .map((r) => fields.find((f) => f.value === r)?.label ?? r)
                        .join(", ")}
                    </strong>
                    . Elegí ese campo en la columna que lo contiene. Un campo
                    personalizado guarda el dato pero no reemplaza al obligatorio.
                  </span>
                </p>
              </div>
            )}
            {/* Colisión en un campo escalar: sin esto el importador se quedaba
                con la primera columna del orden del archivo y descartaba el
                resto en silencio. */}
            {colisiones.length > 0 && (
              <div className="mb-2 rounded border border-vk-danger/40 bg-vk-danger/5 px-2 py-1.5 text-[11px] text-vk-danger">
                {colisiones.map((c) => (
                  <p key={c.target} className="flex gap-1.5">
                    <XCircle className="mt-px h-3 w-3 shrink-0" />
                    <span>
                      {c.columns.length} columnas apuntan a <strong>«{c.label}»</strong>{" "}
                      ({c.columns.join(", ")}) y solo se puede guardar una. Elegí
                      cuál corresponde y mandá las demás a otro campo o a «Ignorar».
                    </span>
                  </p>
                ))}
              </div>
            )}
            {suggestions.map((s) => {
              const target = mappings[s.source_column] ?? "";
              const eff = effectiveStatus(s, target);
              const isCustom = target.startsWith("custom_field:");
              // El target del estado no existe entre las opciones: sin una
              // `<option>` propia el DOM caería a la primera («Sin mapear») y la
              // pantalla mostraría algo distinto de lo que se va a enviar.
              const isUnknown =
                !!target &&
                target !== "ignore" &&
                !isCustom &&
                fields.length > 0 &&
                !fields.some((f) => f.value === target);
              return (
                <div
                  key={s.source_column}
                  className={`grid grid-cols-2 items-start gap-2 px-1 py-1.5 ${
                    eff === "required_missing" ? "bg-vk-danger/5" : ""
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2 pt-1">
                    <StatusDot status={eff} />
                    <span
                      className="truncate font-mono text-xs text-vk-text-primary"
                      title={s.source_column}
                    >
                      {s.source_column}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1">
                    <select
                      value={customFor === s.source_column ? "__custom__" : target}
                      onChange={(e) => selectTarget(s.source_column, e.target.value)}
                      // Por dónde entra el salto del banner de faltantes: dice a
                      // qué hoja pertenece este select y qué campo SUGERÍA
                      // Véktor para esta columna. Se busca por atributo y no por
                      // `id` porque el nombre de la columna es texto libre del
                      // archivo (espacios, acentos, comillas).
                      data-sheet={context.context_id}
                      data-suggests={s.target_field ?? ""}
                      // Deshabilitado —no vacío— mientras carga el catálogo: un
                      // select sin opciones es justamente el fallo que se está
                      // corrigiendo (mostraría "Sin mapear" sobre un target real).
                      disabled={loadingCatalog}
                      className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-xs text-vk-text-primary focus:border-vk-blue focus:outline-none disabled:opacity-50"
                    >
                      <option value="">Sin mapear</option>
                      <option value="ignore">— Ignorar —</option>
                      {fields.map((f) => (
                        <option key={f.value} value={f.value}>
                          {f.label}
                        </option>
                      ))}
                      {isCustom && <option value={target}>{target}</option>}
                      {isUnknown && (
                        <option value={target}>{target} (campo desconocido)</option>
                      )}
                      <option value="__custom__">+ Campo personalizado…</option>
                    </select>
                    {customFor === s.source_column && (
                      <div className="flex gap-1">
                        <input
                          type="text"
                          autoFocus
                          value={customKey}
                          onChange={(e) => setCustomKey(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") commitCustom(s.source_column);
                          }}
                          placeholder="nombre_del_campo"
                          className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-[11px] text-vk-text-primary placeholder:text-vk-text-muted focus:border-vk-blue focus:outline-none"
                        />
                        <button
                          type="button"
                          onClick={() => commitCustom(s.source_column)}
                          disabled={!customKey.trim()}
                          className="shrink-0 rounded bg-vk-blue px-2 py-1 text-[11px] font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50"
                        >
                          OK
                        </button>
                      </div>
                    )}
                    <AmbiguityHint
                      suggestion={s}
                      fields={fields}
                      onPick={(t) => selectTarget(s.source_column, t)}
                    />
                    {/* A3: source + confidence (mismo criterio que el flujo single). */}
                    {eff === "mapped" && s.source !== "none" && (
                      <p className="text-[10px] text-vk-text-muted">
                        {SOURCE_LABELS[s.source] ?? s.source} ·{" "}
                        <span className={confidenceColor(s.confidence)}>
                          {Math.round(s.confidence * 100)}%
                        </span>
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ))}

      {/* Origen del stock de ESTA hoja. Antes era una única pregunta al pie del
          formulario, sin decir a qué hoja aplicaba — imposible de responder bien
          en un archivo con un catálogo y dos libros diarios. */}
      {included && entity === "product" && (
        <StockTreatmentChoice
          value={stockTreatment}
          onChange={(v) => onStockTreatmentChange(context.context_id, v)}
          className="border-t border-vk-border-w bg-vk-bg-light/40 px-3 py-2.5"
        />
      )}

      {/* F-H3.e: eje SEPARADO del de arriba. Aquel decide si el stock inicial
          del catálogo genera un gasto (contable); éste, qué pasa con las
          unidades. Sin este control, `historical_replay` sólo se alcanzaba por
          API y el archivo entraba siempre con el default de cada hoja. */}
      {/* F-H6.b: sólo cuando la hoja declara envío y NO declara comprobante.
          Con comprobante, Véktor agrupa solo y no hay nada que preguntar. */}
      {included &&
        Object.values(mappings).includes("shipping_cost") &&
        !Object.values(mappings).includes("invoice_number") && (
          <ShippingDecisionChoice
            value={shippingValue}
            onChange={(v) => onShippingChange(context.context_id, v)}
            className="border-t border-vk-border-w bg-vk-bg-light/40 px-3 py-2.5"
          />
        )}

      {/* F-H6.c: aparece sólo si la hoja mapea alguna columna que ajusta el
          costo. Sin esas columnas no hay nada que preguntar. */}
      {included && (
        <PurchaseCostChoice
          base={costoValue.base}
          sharedShipping={costoValue.shared}
          lineShipping={costoValue.line}
          onBaseChange={(v) => onCostoChange(context.context_id, { base: v })}
          onSharedShippingChange={(v) =>
            onCostoChange(context.context_id, { shared: v })
          }
          onLineShippingChange={(v) => onCostoChange(context.context_id, { line: v })}
          mostrarAjustes={
            Object.values(mappings).includes("discount") ||
            Object.values(mappings).includes("taxes")
          }
          // F-H6.d: mapear el envío es condición necesaria pero no suficiente —
          // quien decide si un reparto es posible es el servidor. Ofrecerlo por
          // el solo hecho de tener la columna dejaría al usuario eligiendo algo
          // que el importador no va a hacer.
          mostrarEnvioCompartido={
            Object.values(mappings).includes("shipping_cost") &&
            grupos?.puede_distribuir === true
          }
          mostrarFleteDeLinea={Object.values(mappings).includes("shipping_cost_line")}
          className="border-t border-vk-border-w bg-vk-bg-light/40 px-3 py-2.5"
        />
      )}

      {/* F-H6.d: el reparto es la parte del costo que no se puede auditar después
          —el envío queda adentro del costo unitario—, así que se muestra antes
          de confirmar, con los números que devolvió el servidor. */}
      {included && grupos && (
        <PurchaseGroupsPreview
          hoja={grupos}
          className="border-t border-vk-border-w bg-vk-bg-light/40 px-3 py-2.5"
        />
      )}

      {included && inventoryEffect && (
        <InventoryEffectChoice
          hoja={inventoryEffect}
          value={effectValue}
          onChange={(v) => onEffectChange(context.context_id, v)}
          className="border-t border-vk-border-w bg-vk-bg-light/40 px-3 py-2.5"
        />
      )}
    </div>
  );
}

function MultiContextMapper({
  fileId,
  contexts,
  contextualRisk,
  masterPreviews,
  parserWarnings,
  onDone,
}: {
  fileId: string;
  contexts: MappingContext[];
  parserWarnings: string[];
  contextualRisk: ContextualColumnRisk[];
  masterPreviews: MasterPreviewSummary[];
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  // Una hoja arranca tildada SOLO si Véktor supo qué es. Las que no pudo
  // clasificar arrancan afuera: antes entraban todas tildadas y, sin entidad,
  // caían a "sale" — así una hoja de resúmenes derivados del Libro Diario se
  // importaba como 1840 ventas sin que nadie lo decidiera.
  const [included, setIncluded] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(contexts.map((c) => [c.context_id, c.entity_type != null])),
  );
  // entity_type efectivo por contexto (reasignable en texto/imagen y en las
  // hojas que el backend no pudo clasificar). Sin default a "sale".
  const [entityByCtx, setEntityByCtx] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      contexts.map((c) => [c.context_id, c.entity_type ?? ENTITY_UNSET]),
    ),
  );
  // Origen del stock POR HOJA. Un único valor para todo el archivo obligaba a
  // mentir cuando venían juntos un catálogo que el negocio ya tenía y una hoja
  // de compras: elegir «Lo compré» generaba COGS por productos que ya figuran
  // como egresos del libro diario (doble conteo).
  const [stockTreatmentByCtx, setStockTreatmentByCtx] = useState<
    Record<string, StockTreatment>
  >({});
  const handleStockTreatmentChange = useCallback(
    (ctxId: string, value: StockTreatment) =>
      setStockTreatmentByCtx((prev) => ({ ...prev, [ctxId]: value })),
    [],
  );
  // F-H3.e: efecto de inventario ELEGIDO por hoja. Sólo lo que el usuario tocó;
  // el default lo pone el backend y se aplica al render (`efectoDe`), no
  // copiándolo acá: los defaults cambian mientras se mapea, y un estado
  // inicializado una vez quedaría mostrando un modo que ya no corresponde.
  const [effectByCtx, setEffectByCtx] = useState<Record<string, string>>({});
  const handleEffectChange = useCallback(
    (ctxId: string, value: string) =>
      setEffectByCtx((prev) => ({ ...prev, [ctxId]: value })),
    [],
  );
  // F-H6.b: decisión de envío por hoja. Sólo lo que el usuario eligió: la
  // ausencia ES la decisión por default (no registrar), así que no se
  // inicializa con nada.
  // F-H6.c: la decisión de costo de cada hoja. Sólo se manda la de las hojas que
  // se apartaron de un default — mandar los defaults sería ruido, y el backend ya
  // los aplica igual.
  const [costoByCtx, setCostoByCtx] = useState<
    Record<
      string,
      {
        base: PurchaseCostBase;
        shared: PurchaseSharedShipping;
        line: PurchaseLineShipping;
      }
    >
  >({});
  const [shippingByCtx, setShippingByCtx] = useState<
    Record<string, ShippingAction | null>
  >({});
  const handleShippingChange = useCallback(
    (ctxId: string, value: ShippingAction | null) =>
      setShippingByCtx((prev) => ({ ...prev, [ctxId]: value })),
    [],
  );
  const mappingsRef = useRef<Record<string, Record<string, string>>>({});
  // F8c: decisiones de columnas riesgosas + touched-set (user_selected).
  const [riskDecisions, setRiskDecisions] = useState<ColumnRiskDecision[]>([]);
  const touchedRef = useRef<Set<string>>(new Set());
  // Los mapeos viven en mappingsRef (no en estado) para evitar re-renders de las
  // hojas; este contador los hace observables para recalcular el riesgo en vivo.
  const [mappingsVersion, setMappingsVersion] = useState(0);

  const handleIncludeChange = useCallback((ctxId: string, inc: boolean) => {
    setIncluded((prev) => ({ ...prev, [ctxId]: inc }));
  }, []);

  const handleEntityChange = useCallback((ctxId: string, ent: string) => {
    setEntityByCtx((prev) => ({ ...prev, [ctxId]: ent }));
  }, []);

  const handleMappingsChange = useCallback(
    (ctxId: string, mappings: Record<string, string>) => {
      mappingsRef.current[ctxId] = mappings;
      setMappingsVersion((v) => v + 1);
    },
    [],
  );

  // Problemas por hoja (requerido descubierto / colisión escalar). Los reporta
  // cada sección con las MISMAS reglas que valida el confirm del backend, así el
  // bloqueo se ve acá y no aparece recién como un 422.
  const [issuesByCtx, setIssuesByCtx] = useState<Record<string, SheetIssues>>({});
  const handleIssuesChange = useCallback((ctxId: string, issues: SheetIssues) => {
    setIssuesByCtx((prev) => {
      const anterior = prev[ctxId];
      const igual =
        anterior !== undefined &&
        JSON.stringify(anterior) === JSON.stringify(issues);
      return igual ? prev : { ...prev, [ctxId]: issues };
    });
  }, []);

  // F8c: cambio MANUAL de un mapeo en cualquier hoja → marca la columna como
  // tocada por el usuario y dispara el recompute (via mappingsVersion).
  const handleColumnTouched = useCallback((ctxId: string, col: string) => {
    touchedRef.current.add(riskKey(ctxId, col));
    setMappingsVersion((v) => v + 1);
  }, []);

  // F-A: lado de LECTURA del mismo set. Que sea un ref —y por lo tanto no
  // dispare re-render— está bien: se consulta dentro del efecto que re-inicializa
  // el mapeo, que ya corre cuando cambian la sección o las sugerencias.
  const isColumnTouched = useCallback(
    (ctxId: string, col: string) => touchedRef.current.has(riskKey(ctxId, col)),
    [],
  );

  const { byContext: warningsByCtx, general: generalWarnings } = useMemo(
    () => splitWarningsByContext(parserWarnings, contexts),
    [parserWarnings, contexts],
  );

  // Mismo catálogo que usan las secciones (react-query dedupea por `queryKey`):
  // acá alimenta el banner con el nombre y el motivo de cada requerido.
  const { data: catalogoDeCampos } = useFieldCatalog();

  // Sección efectiva de una hoja: la que eligió el usuario, si no la que trajo
  // el backend, si no NINGUNA. Fuente única — antes cada lugar repetía el
  // `?? "sale"` y ese default silencioso es el bug que estamos cerrando.
  const entityFor = useCallback(
    (ctx: MappingContext): string =>
      entityByCtx[ctx.context_id] ?? ctx.entity_type ?? ENTITY_UNSET,
    [entityByCtx],
  );

  // Hojas tildadas a las que todavía nadie les asignó sección: bloquean el
  // confirm (el backend también las rechaza, ver el guard del endpoint).
  const sinSeccion = contexts.filter(
    (c) => included[c.context_id] && entityFor(c) === ENTITY_UNSET,
  );

  // Hojas de producto incluidas: cada una declara su propio origen de stock.
  const hojasDeProducto = contexts.filter(
    (c) => included[c.context_id] && entityFor(c) === "product",
  );
  const stockTreatmentPayload = hojasDeProducto.length
    ? Object.fromEntries(
        hojasDeProducto.map((c) => [
          c.context_id,
          stockTreatmentByCtx[c.context_id] ?? "opening_balance",
        ]),
      )
    : undefined;

  // Doble conteo: una hoja marcada como compra genera un COGS por producto. Si
  // el mismo archivo trae gastos, es probable que esos costos ya estén ahí.
  // Avisa, no bloquea — puede ser legítimo (compras que no pasaron por el libro).
  const hojasDeGasto = contexts.filter(
    (c) => included[c.context_id] && entityFor(c) === "expense",
  );
  const hojasMarcadasCompra = hojasDeProducto.filter(
    (c) => (stockTreatmentByCtx[c.context_id] ?? "opening_balance") === "purchase",
  );
  const riesgoDobleConteo =
    hojasDeGasto.length > 0 && hojasMarcadasCompra.length > 0;

  /**
   * F-C: llevar a la persona al DESTINO del campo que falta.
   *
   * La regla es la misma que la de la corrección V10: se señala una columna sólo
   * cuando NO hay ambigüedad. Si entre las sugerencias de la hoja hay
   * exactamente UNA que apuntaba a ese campo, esa es la columna de la que salió
   * el dato y ahí va el foco. Si hay varias —o ninguna— elegir una sería elegir
   * al azar, así que se salta a la tarjeta de la hoja y se la resalta: la
   * persona ve dónde tiene que mirar y decide ella cuál es.
   *
   * Se lee el DOM en vez de mantener un índice en estado porque lo que hay que
   * enfocar es exactamente lo que está renderizado; un índice paralelo puede
   * quedar viejo justo cuando la hoja acaba de cambiar de sección.
   */
  const [hojaResaltada, setHojaResaltada] = useState<string | null>(null);
  const irAlDestino = useCallback((ctxId: string, target: string) => {
    const candidatos = Array.from(
      document.querySelectorAll<HTMLSelectElement>("select[data-sheet][data-suggests]"),
    ).filter((el) => el.dataset.sheet === ctxId && el.dataset.suggests === target);
    const unico = candidatos.length === 1 ? candidatos[0] : undefined;
    if (unico) {
      // `scrollIntoView` no existe en jsdom: el foco es lo que importa y no se
      // puede perder por un salto de scroll que el entorno de test no implementa.
      unico.scrollIntoView?.({ behavior: "smooth", block: "center" });
      unico.focus();
      setHojaResaltada(null);
      return;
    }
    const tarjeta = Array.from(
      document.querySelectorAll<HTMLElement>("[data-sheet-card]"),
    ).find((el) => el.dataset.sheetCard === ctxId);
    tarjeta?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    setHojaResaltada(ctxId);
  }, []);

  // Solo cuentan las hojas INCLUIDAS: una destildada no se importa, así que sus
  // problemas no pueden bloquear nada.
  const hojasConProblemas = contexts
    .filter((c) => included[c.context_id])
    .map((c) => ({
      ctxId: c.context_id,
      label: c.label ?? c.context_id,
      issues: issuesByCtx[c.context_id],
      // El motivo de cada requerido depende de la ENTIDAD de la hoja: la misma
      // fecha se pierde distinto en una venta que en un gasto.
      faltantes: explainMissing(
        issuesByCtx[c.context_id]?.missingRequired ?? [],
        catalogoDeCampos?.[entityFor(c)],
      ),
    }))
    .filter(
      (h): h is typeof h & { issues: SheetIssues } =>
        !!h.issues &&
        (h.issues.missingRequired.length > 0 || h.issues.collisions.length > 0),
    );

  // F8c: input del recompute de riesgo — recorre las hojas incluidas leyendo el
  // mapeo vivo de mappingsRef (observable via mappingsVersion) y el touched-set.
  // touchedRef/mappingsRef son refs → se leen en el momento; mappingsVersion en
  // deps garantiza que el memo se rearme cuando cambia un mapeo de cualquier hoja.
  const riskRecomputeInput = useMemo(() => {
    const columnMappings: ColumnMapping[] = [];
    const contextEntity: Record<string, string> = {};
    for (const ctx of contexts) {
      if (!included[ctx.context_id]) continue;
      const ent = entityFor(ctx);
      if (!ent) continue;
      contextEntity[ctx.context_id] = ent;
      const m = mappingsRef.current[ctx.context_id] ?? {};
      for (const [src, target] of Object.entries(m)) {
        if (!target) continue;
        columnMappings.push({
          source_column: src,
          target_field: target,
          context_id: ctx.context_id,
          entity_type: ent,
          user_selected: touchedRef.current.has(riskKey(ctx.context_id, src)),
        });
      }
    }
    return {
      columnMappings,
      contextEntity,
      confirmedFields: {} as Record<string, boolean>,
      contextConfirmed: included,
    };
    // mappingsVersion es un trigger deliberado: el memo lee mappingsRef.current
    // (no rastreable por React), así que dependemos del contador para rearmarlo.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contexts, included, entityFor, mappingsVersion]);

  const riskRecomputeKey = useMemo(
    () => JSON.stringify(riskRecomputeInput),
    [riskRecomputeInput],
  );

  const recomputeRisk = useCallback(
    (signal: AbortSignal) =>
      ingestionService.recomputeColumnRisk(fileId, riskRecomputeInput, signal),
    [fileId, riskRecomputeInput],
  );

  // F-H3.e: se re-consulta cuando cambia el mapeo — mapear `cantidad` es lo que
  // habilita que una hoja pueda aplicar su historia al stock.
  const { data: inventoryEffects = [] } = useQuery({
    queryKey: ["inventory-effects", fileId, riskRecomputeKey],
    queryFn: ({ signal }) =>
      ingestionService.fetchInventoryEffects(fileId, riskRecomputeInput, signal),
    // La clave incluye el mapeo, así que cambia con cada edición. Sin conservar
    // lo anterior, el selector DESAPARECE en cada recálculo —y el confirm que
    // caiga en esa ventana viaja sin efecto declarado, que es peor que el
    // parpadeo—. Lo que se muestra mientras tanto es el modo de un mapeo
    // ligeramente viejo; la respuesta nueva lo corrige apenas llega.
    placeholderData: (prev) => prev,
  });
  const effectsByCtx = useMemo(
    () => Object.fromEntries(inventoryEffects.map((h) => [h.context_id, h])),
    [inventoryEffects],
  );
  /**
   * Modo EFECTIVO de una hoja: lo que eligió el usuario si sigue estando entre
   * las opciones, y si no el default de Véktor. La comprobación importa: sacar
   * la columna de cantidad puede dejar a la hoja sin poder mover inventario, y
   * mandar el modo viejo sería mandar algo que el backend ya no ofrece.
   */
  const efectoDe = useCallback(
    (ctxId: string): string => {
      const hoja = effectsByCtx[ctxId];
      if (!hoja) return "";
      const elegido = effectByCtx[ctxId];
      return elegido && hoja.options.some((o) => o.value === elegido)
        ? elegido
        : hoja.default;
    },
    [effectsByCtx, effectByCtx],
  );

  /**
   * F-H6.d: las decisiones de costo tal como las eligió el usuario, para PEDIR
   * el reparto.
   *
   * Se manda el estado CRUDO y no el efectivo a propósito: el efectivo depende
   * de `puede_distribuir`, que es justo lo que devuelve esta consulta. Derivarlo
   * antes de preguntar cerraría el círculo sobre sí mismo. El confirm sí manda
   * el efectivo — ahí ya se sabe qué puede hacer el importador.
   */
  const costoDraft = useMemo(
    () =>
      Object.entries(costoByCtx)
        .filter(([ctxId]) => included[ctxId])
        .map(([ctxId, d]) => ({
          context_id: ctxId,
          base: d.base,
          shared_shipping: d.shared,
          line_shipping: d.line,
        })),
    [costoByCtx, included],
  );
  /**
   * F-H6.b: sólo las hojas donde el usuario eligió. Sin entrada, sus envíos sin
   * comprobante no se registran — que es el default seguro.
   *
   * Se calcula acá y no adentro del confirm porque el PREVIEW del reparto
   * también depende de esto: una hoja sin número de comprobante puede formar un
   * grupo si el usuario declaró «toda la hoja es una compra», y mostrarla como
   * no repartible sería contradecir lo que ya eligió.
   */
  const envioPayload = useMemo(
    () =>
      Object.entries(shippingByCtx)
        .filter(([ctxId, action]) => action !== null && included[ctxId])
        .map(([ctxId, action]) => ({
          context_id: ctxId,
          action: action as ShippingAction,
        })),
    [shippingByCtx, included],
  );
  const draftKey = useMemo(
    () => JSON.stringify([costoDraft, envioPayload]),
    [costoDraft, envioPayload],
  );
  // Sin ninguna columna de envío compartido no hay reparto posible: se evita una
  // consulta por archivo que no la necesita.
  const hayEnvioCompartido = riskRecomputeInput.columnMappings.some(
    (m) => m.target_field === "shipping_cost",
  );
  // Se recuerda aunque cambie la clave: `error` de react-query vuelve a `null`
  // con cada clave nueva, y la compuerta es del TENANT, no del mapeo.
  const [sinMotor, setSinMotor] = useState(false);
  const { data: purchaseGroups = [], error: errorGrupos } = useQuery({
    queryKey: ["purchase-groups", fileId, riskRecomputeKey, draftKey],
    queryFn: ({ signal }) =>
      ingestionService.fetchPurchaseGroups(
        fileId,
        {
          ...riskRecomputeInput,
          shippingDecisions: envioPayload,
          purchaseCostDecisions: costoDraft,
        },
        signal,
      ),
    enabled: hayEnvioCompartido && !sinMotor,
    // Mismo motivo que en `/inventory-effects`: la clave cambia con cada edición
    // del mapeo, y sin conservar lo anterior el selector del tercer eje
    // desaparecería en cada recálculo.
    placeholderData: (prev) => prev,
    retry: false,
  });
  useEffect(() => {
    if (esMotorDeCostosDeshabilitado(errorGrupos)) setSinMotor(true);
  }, [errorGrupos]);
  const gruposByCtx = useMemo(
    () => Object.fromEntries(purchaseGroups.map((h) => [h.context_id, h])),
    [purchaseGroups],
  );
  /**
   * Reparto EFECTIVO de una hoja: lo que eligió el usuario sólo si el servidor
   * dice que esa hoja se puede repartir; si no, el default que no toca ningún
   * costo. Mismo criterio que `efectoDe`: mandar un modo que el backend ya no
   * ofrece sería mandar algo que no va a pasar.
   */
  const compartidoDe = useCallback(
    (ctxId: string): PurchaseSharedShipping =>
      costoByCtx[ctxId]?.shared === "por_subtotal" &&
      gruposByCtx[ctxId]?.puede_distribuir
        ? "por_subtotal"
        : "no_distribuir",
    [costoByCtx, gruposByCtx],
  );

  const { registrar: registrarImpacto, vista: vistaImpacto } =
    useImpactoPostConfirm(onDone, fileId);

  const confirmMutation = useMutation({
    mutationFn: () => {
      const columnMappings: ColumnMapping[] = [];
      const contextEntity: Record<string, string> = {};
      for (const ctx of contexts) {
        if (!included[ctx.context_id]) continue;
        const ent = entityFor(ctx);
        // Sin sección elegida la hoja no viaja. El botón ya lo impide; esto es
        // la red por si el estado cambia entre el render y el click.
        if (!ent) continue;
        // Se manda SIEMPRE, no solo en texto/imagen: es la sección que el
        // usuario vio en pantalla, y el backend la toma como override
        // autoritativo. Mandarla solo a veces dejaba que lo importado
        // divergiera de lo que el panel mostraba.
        contextEntity[ctx.context_id] = ent;
        const m = mappingsRef.current[ctx.context_id] ?? {};
        for (const [src, target] of Object.entries(m)) {
          if (!target) continue;
          columnMappings.push({
            source_column: src,
            target_field: target,
            context_id: ctx.context_id,
            entity_type: ent,
            user_selected: touchedRef.current.has(riskKey(ctx.context_id, src)),
          });
        }
      }
      // F-H3.e: el efecto SÓLO de las hojas que viajan. Mandar el de una hoja
      // excluida da 422 ("apunta a una hoja que no está en el archivo"), porque
      // el backend arma los perfiles con los mapeos recibidos.
      const inventoryEffectPayload: Record<string, string> = {};
      for (const ctxId of Object.keys(contextEntity)) {
        const modo = efectoDe(ctxId);
        if (modo) inventoryEffectPayload[ctxId] = modo;
      }
      // F-H6.b: sólo las hojas donde el usuario eligió. Sin entrada, sus envíos
      // sin comprobante no se registran — que es el default seguro.
      // F-H6.c: se manda sólo lo que el usuario cambió. Una hoja con los dos ejes
      // en su default se comporta igual mandándola o no, y omitirla deja claro en
      // la traza que no hubo decisión.
      const costoPayload: PurchaseCostDecision[] = Object.entries(costoByCtx)
        .filter(
          ([ctxId, d]) =>
            included[ctxId] &&
            (d.base !== "monto_incluye" ||
              compartidoDe(ctxId) !== "no_distribuir" ||
              d.line !== "gasto_aparte"),
        )
        .map(([ctxId, d]) => ({
          context_id: ctxId,
          base: d.base,
          // Efectivo, no lo crudo: si el servidor dice que esta hoja no se puede
          // repartir, declarar `por_subtotal` sería pedir algo imposible.
          shared_shipping: compartidoDe(ctxId),
          line_shipping: d.line,
        }));
      return ingestionService.confirmFile(
        fileId,
        {},
        columnMappings,
        included,
        contextEntity,
        // Dict {context_id: tratamiento} solo con las hojas de producto
        // INCLUIDAS; `undefined` si no hay ninguna (el backend asume apertura).
        stockTreatmentPayload,
        riskDecisions,
        Object.keys(inventoryEffectPayload).length > 0
          ? inventoryEffectPayload
          : undefined,
        envioPayload,
        costoPayload,
      );
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      void queryClient.invalidateQueries({ queryKey: ["column-mappings-learned"] });
      // Avisos human-in-the-loop: el panel se cierra en onDone(), así que van como
      // toasts (compras sin proveedor/producto, filas a "Otros").
      for (const w of result.warnings ?? []) toast(w, "warning");
      // F-H3.c: con impacto sobre el inventario el panel queda abierto para
      // mostrarlo; sin impacto se cierra como siempre.
      if (registrarImpacto(result)) return;
      onDone();
    },
    onError: (error) => {
      // 409 (concurrente/ya importado) o timeout (import en curso): no es un error
      // de mapeo — se avisa amable y se refresca la lista, sin banner rojo.
      handleTransientConfirmError(error, toast, () =>
        queryClient.invalidateQueries({ queryKey: ["ingestion-files"] }),
      );
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => ingestionService.cancelFile(fileId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      onDone();
    },
  });

  const anyIncluded = Object.values(included).some(Boolean);
  const errDetail = (
    confirmMutation.error as { response?: { data?: { detail?: string } } } | null
  )?.response?.data?.detail;

  // F-H3.c: importado y con impacto sobre el stock → mostrarlo en vez del mapeo.
  if (vistaImpacto) return vistaImpacto;

  return (
    <div className="ml-2 mt-3 rounded-xl border border-vk-border-w bg-vk-surface-w p-4">
      <div className="mb-3">
        <p className="text-sm font-semibold text-vk-text-primary">Mapeo por hoja</p>
        <p className="mt-0.5 text-xs text-vk-text-muted">
          {contexts.length} hojas detectadas. Revisá el mapeo de cada una y elegí cuáles importar.
        </p>
      </div>

      {/* Avisos del parser que no se pudieron atribuir a una hoja concreta. */}
      {generalWarnings.length > 0 && (
        <div className="mb-3 rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 px-3 py-2">
          {generalWarnings.map((w) => (
            <p key={w} className="flex gap-1.5 text-[11px] leading-snug text-vk-warning">
              <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
              <span>{w}</span>
            </p>
          ))}
        </div>
      )}

      {/* F7e: preview de maestros (clientes/proveedores) detectados en el archivo. */}
      <MasterPreviewPanel previews={masterPreviews} />

      {/* F8c: panel de decisiones de columnas riesgosas — también en multi-hoja.
          El riesgo se recalcula en vivo (recompute) al cambiar mapeos/inclusión. */}
      {contextualRisk.length > 0 && (
        <div className="mb-3">
          <ColumnRiskDecisionsPanel
            initialRisks={contextualRisk}
            recomputeKey={riskRecomputeKey}
            recompute={recomputeRisk}
            onDecisionsChange={setRiskDecisions}
            onCancelAndComplete={() => cancelMutation.mutate()}
          />
        </div>
      )}

      <div className="space-y-3">
        {contexts.map((ctx) => (
          <SheetMapperSection
            key={ctx.context_id}
            fileId={fileId}
            context={ctx}
            included={included[ctx.context_id] ?? false}
            entity={entityFor(ctx)}
            warnings={warningsByCtx[ctx.context_id] ?? []}
            onIncludeChange={handleIncludeChange}
            onMappingsChange={handleMappingsChange}
            onEntityChange={handleEntityChange}
            onColumnTouched={handleColumnTouched}
            isColumnTouched={isColumnTouched}
            onIssuesChange={handleIssuesChange}
            stockTreatment={stockTreatmentByCtx[ctx.context_id] ?? "opening_balance"}
            onStockTreatmentChange={handleStockTreatmentChange}
            inventoryEffect={effectsByCtx[ctx.context_id]}
            effectValue={efectoDe(ctx.context_id)}
            onEffectChange={handleEffectChange}
            shippingValue={shippingByCtx[ctx.context_id] ?? null}
            grupos={gruposByCtx[ctx.context_id]}
            resaltada={hojaResaltada === ctx.context_id}
            costoValue={{
              base: costoByCtx[ctx.context_id]?.base ?? "monto_incluye",
              // El efectivo, no el crudo: el radio tiene que mostrar lo que
              // realmente se va a mandar, no una elección que el servidor ya
              // dijo que no puede honrar.
              shared: compartidoDe(ctx.context_id),
              line: costoByCtx[ctx.context_id]?.line ?? "gasto_aparte",
            }}
            onCostoChange={(ctxId, patch) =>
              setCostoByCtx((prev) => ({
                ...prev,
                [ctxId]: {
                  base: patch.base ?? prev[ctxId]?.base ?? "monto_incluye",
                  shared: patch.shared ?? prev[ctxId]?.shared ?? "no_distribuir",
                  line: patch.line ?? prev[ctxId]?.line ?? "gasto_aparte",
                },
              }))
            }
            onShippingChange={handleShippingChange}
          />
        ))}
      </div>

      {/* Doble conteo: el COGS de una hoja marcada como compra puede ya estar
          cargado como egreso en las hojas de gastos del mismo archivo. */}
      {riesgoDobleConteo && (
        <p className="mt-3 flex gap-1.5 text-xs text-vk-warning">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
          <span>
            {hojasMarcadasCompra.map((c) => `«${c.label ?? c.context_id}»`).join(", ")}{" "}
            {hojasMarcadasCompra.length !== 1 ? "están marcadas" : "está marcada"} como
            compra, y este archivo también trae gastos. Si esas compras ya figuran
            ahí, el costo de la mercadería se va a contar dos veces.
          </span>
        </p>
      )}

      {confirmMutation.isError &&
        !isTransientConfirmError(confirmMutation.error) && (
          <p className="mt-3 text-xs text-vk-danger">
            {errDetail ?? "Error al confirmar. Revisá los campos requeridos de cada hoja."}
          </p>
        )}

      {sinSeccion.length > 0 && (
        <p className="mt-3 flex gap-1.5 text-xs text-vk-warning">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
          <span>
            Elegí a qué sección {sinSeccion.length !== 1 ? "van" : "va"}{" "}
            {sinSeccion.map((c) => `«${c.label}»`).join(", ")}, o destildá
            {sinSeccion.length !== 1 ? "las" : "la"} para no importar
            {sinSeccion.length !== 1 ? "las" : "la"}.
          </span>
        </p>
      )}

      {/* Bloqueo por hoja, con las mismas reglas que valida el confirm: sin esto
          el usuario descubría el problema recién con un 422 que no decía cómo
          salir (incidente ASTERIA: tres confirms rechazados seguidos). */}
      {hojasConProblemas.length > 0 && (
        <div className="mt-3 space-y-2">
          {hojasConProblemas.map(({ ctxId, label, issues, faltantes }) => (
            <div key={ctxId} className="text-xs text-vk-danger">
              <p className="flex gap-1.5">
                <XCircle className="mt-px h-3.5 w-3.5 shrink-0" />
                <span>
                  <strong>«{label}»</strong>:{" "}
                  {[
                    faltantes.length === 1
                      ? "falta un dato obligatorio"
                      : faltantes.length > 1
                        ? "faltan datos obligatorios"
                        : null,
                    // Las dos cosas pueden pasar a la vez y antes la segunda se
                    // perdía: el mensaje era un o-lo-uno-o-lo-otro y la hoja
                    // seguía bloqueada por algo que el banner no nombró.
                    issues.collisions.length > 0
                      ? "hay dos columnas para el mismo campo"
                      : null,
                  ]
                    .filter(Boolean)
                    .join(" y ")}
                  .{issues.collisions.length > 0 && " Revisá la hoja más arriba."}
                </span>
              </p>
              {/* Cada faltante se nombra, se explica QUÉ SE PIERDE la persona si
                  no lo mapea, y lleva al lugar donde se arregla. Antes esto
                  decía «falta un dato obligatorio, revisá la hoja más arriba»:
                  no decía cuál, no decía por qué, y dejaba el botón apagado sin
                  una acción concreta. */}
              {faltantes.length > 0 && (
                <ul className="mt-1 space-y-1 pl-5">
                  {faltantes.map((f) => (
                    <li key={f.field}>
                      <button
                        type="button"
                        onClick={() => irAlDestino(ctxId, f.field)}
                        className="text-left underline decoration-dotted underline-offset-2 hover:decoration-solid focus:outline-none focus-visible:ring-1 focus-visible:ring-vk-danger"
                      >
                        <strong>{f.label}</strong> — ir a la hoja
                      </button>
                      {f.reason && (
                        <span className="mt-0.5 block text-[11px] leading-snug text-vk-text-secondary">
                          {f.reason}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="mt-4 flex items-center gap-2">
        <button
          type="button"
          onClick={() => confirmMutation.mutate()}
          disabled={
            confirmMutation.isPending ||
            !anyIncluded ||
            sinSeccion.length > 0 ||
            hojasConProblemas.length > 0
          }
          className="flex items-center gap-1.5 rounded-lg bg-vk-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
        >
          <CheckCircle className="h-3.5 w-3.5" />
          {confirmMutation.isPending ? "Confirmando..." : "Confirmar importación"}
        </button>
        <button
          type="button"
          onClick={() => cancelMutation.mutate()}
          disabled={cancelMutation.isPending || confirmMutation.isPending}
          className="rounded-lg border border-vk-border-w px-3 py-1.5 text-xs text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-50 transition-colors"
        >
          Cancelar
        </button>
      </div>
    </div>
  );
}

interface ColumnMapperPanelProps {
  fileId: string;
  onDone: () => void;
}

export function ColumnMapperPanel({ fileId, onDone }: ColumnMapperPanelProps) {
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const [confirmedFields, setConfirmedFields] = useState({
    ventas: true,
    gastos: false,
    productos: false,
    // F7e: buckets de maestros — mismo criterio que gastos/productos (el
    // usuario los tilda a propósito, no vienen prendidos por default).
    clientes: false,
    proveedores: false,
  });
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [unmappedQueue, setUnmappedQueue] = useState<string[]>([]);
  const [showUnmappedModal, setShowUnmappedModal] = useState(false);
  const [initialized, setInitialized] = useState(false);
  // F8c: decisiones de columnas riesgosas + touched-set (user_selected). El
  // touched-set solo se marca en cambios MANUALES de mapeo (no en la
  // inicialización desde sugerencias, que es señal débil de F7).
  const [riskDecisions, setRiskDecisions] = useState<ColumnRiskDecision[]>([]);
  const touchedRef = useRef<Set<string>>(new Set());
  // A3: cuando el tipo quedó ambiguo ("general"), el usuario elige el propósito aquí.
  const [purpose, setPurpose] = useState<string>("");
  // A: tratamiento del stock cuando el archivo trae productos (default: saldo de apertura).
  const [stockTreatment, setStockTreatment] =
    useState<StockTreatment>("opening_balance");

  const { data: preview } = useQuery({
    queryKey: ["ingestion-preview", fileId],
    queryFn: () => ingestionService.getPreview(fileId),
    retry: false,
  });

  // Derivar entity_type desde el inferred_type del archivo (no de los checkboxes).
  // Los checkboxes controlan qué importar, no el tipo de schema para mapear.
  const summary = preview?.parsed_summary_json as Record<string, unknown> | null | undefined;
  const _inferredType = typeof summary?.inferred_type === "string" ? summary.inferred_type : "";
  const INFERRED_TO_ENTITY: Record<string, string> = {
    ventas: "sale",
    gastos: "expense",
    stock: "product",
    // F7e: archivo legacy de un solo contexto ya inferido como maestro
    // (ver _build_master_previews en el backend).
    clientes: "customer",
    proveedores: "supplier",
  };
  // A3: archivo ambiguo ("general") → el tipo efectivo es el propósito elegido.
  const isAmbiguous = _inferredType === "general";
  const effectiveInferred = isAmbiguous ? purpose : _inferredType;
  const needsPurpose = isAmbiguous && !purpose;
  const entityType = INFERRED_TO_ENTITY[effectiveInferred] ?? "sale";
  // F8c: riesgo contextual por columna (reemplaza el legacy columns_at_risk).
  const contextualRisk = preview?.contextual_column_risk ?? [];
  // Lo que el parser detectó sobre el archivo (hojas derivadas, movimientos
  // ambiguos). Se muestra junto a la hoja que lo generó, no en un bloque suelto.
  const parserWarnings = Array.isArray(summary?.warnings)
    ? (summary.warnings as unknown[]).filter(
        (w): w is string => typeof w === "string",
      )
    : [];
  // A: el archivo trae stock si el schema efectivo es de productos, si se tildó
  // "productos", o si el summary detectó stock/productos.
  const hasStock =
    entityType === "product" ||
    confirmedFields.productos ||
    summaryHasStock(summary);

  // F8c: input/clave/closure del recompute de riesgo en vivo. touchedRef es un
  // ref (no está en deps): se lee en el momento del recompute — cuando el usuario
  // toca un mapeo, `mappings` cambia, dispara el recompute y re-lee el touched-set.
  const riskRecomputeInput = useMemo(
    () => ({
      columnMappings: Object.entries(mappings)
        .filter(([, t]) => Boolean(t))
        .map(([src, target]) => ({
          source_column: src,
          target_field: target,
          context_id: "table",
          entity_type: entityType,
          user_selected: touchedRef.current.has(riskKey("table", src)),
        })) as ColumnMapping[],
      contextEntity: {} as Record<string, string>,
      confirmedFields,
      contextConfirmed: {} as Record<string, boolean>,
    }),
    [mappings, confirmedFields, entityType],
  );
  const riskRecomputeKey = useMemo(
    () => JSON.stringify(riskRecomputeInput),
    [riskRecomputeInput],
  );
  const recomputeRisk = useCallback(
    (signal: AbortSignal) =>
      ingestionService.recomputeColumnRisk(fileId, riskRecomputeInput, signal),
    [fileId, riskRecomputeInput],
  );

  // F-H3.e: mismo contrato que en el camino multi-hoja. Este camino manda sus
  // mapeos con `context_id: "table"`, así que también tiene una hoja a la que
  // declararle un efecto — sin esto, un archivo de una sola tabla entraba
  // siempre con el default y nunca podía aplicar su historia.
  const { data: inventoryEffects = [] } = useQuery({
    queryKey: ["inventory-effects", fileId, riskRecomputeKey],
    queryFn: ({ signal }) =>
      ingestionService.fetchInventoryEffects(fileId, riskRecomputeInput, signal),
    // La clave incluye el mapeo, así que cambia con cada edición. Sin conservar
    // lo anterior, el selector DESAPARECE en cada recálculo —y el confirm que
    // caiga en esa ventana viaja sin efecto declarado, que es peor que el
    // parpadeo—. Lo que se muestra mientras tanto es el modo de un mapeo
    // ligeramente viejo; la respuesta nueva lo corrige apenas llega.
    placeholderData: (prev) => prev,
  });
  const hojaEfecto = inventoryEffects[0];
  const [effectElegido, setEffectElegido] = useState<string | null>(null);
  /**
   * Este camino NO puede traer costos de compra, y por eso no los ofrece.
   *
   * El importador plano no cobra el envío ni aplica las decisiones de costo —el
   * cobro vive en un closure del camino multi-hoja y la decisión se busca bajo
   * otra clave—, así que el confirm rechaza el archivo con 422 en cuanto ve una
   * columna de envío mapeada O una decisión de costo declarada. Arreglar el
   * camino plano de verdad es otra fase.
   *
   * Mientras tanto, lo que la pantalla NO puede hacer es ofrecer los tres ejes
   * acá: cada decisión que tomara terminaría en un rechazo, y antes de ese guard
   * terminaba en algo peor —el import aceptaba el archivo y la ignoraba en
   * silencio, dejando el costo más bajo que el real y el margen inflado—. Se
   * nombra el problema y se dicen las dos salidas, que son las mismas del 422.
   *
   * El predicado del backend (`_plano`) es más AMPLIO que este `!isMultiContext`,
   * así que todo lo que la pantalla manda por acá cae adentro de su rechazo: no
   * hay archivo que la UI deje pasar y el confirm frene por sorpresa.
   */
  const columnasDeCostoEnTablaUnica = [
    ...new Set(
      riskRecomputeInput.columnMappings
        .filter(
          (m) =>
            m.target_field === "shipping_cost" ||
            m.target_field === "shipping_cost_line",
        )
        .map((m) => m.source_column),
    ),
  ];
  // Lo elegido sólo vale mientras siga ofreciéndose: sacar la columna de
  // cantidad puede dejar a la hoja sin poder mover inventario.
  const efectoActual =
    effectElegido && hojaEfecto?.options.some((o) => o.value === effectElegido)
      ? effectElegido
      : (hojaEfecto?.default ?? "");

  function choosePurpose(value: string) {
    setPurpose(value);
    const opt = PURPOSE_OPTIONS.find((o) => o.value === value);
    if (opt) {
      setConfirmedFields({
        ventas: false,
        gastos: false,
        productos: false,
        clientes: false,
        proveedores: false,
        [opt.field]: true,
      });
    }
    // Re-inicializar el mapeo con el nuevo schema de entidad.
    setInitialized(false);
    setMappings({});
    // F8c: el mapeo se re-deriva de las sugerencias del nuevo schema — ninguna
    // columna quedó tocada a mano en el schema nuevo (evita fuga de user_selected).
    touchedRef.current.clear();
  }

  // Multi-contexto: se usa el mapeo por contexto cuando hay >1 contexto (multi-hoja)
  // o cuando el único contexto NO es una tabla (texto/imagen). Una sola tabla
  // (csv / xlsx 1 hoja) sigue por el flujo legacy (mapeo plano → insert single-sheet).
  const contexts = (
    Array.isArray(summary?.mapping_contexts) ? summary.mapping_contexts : []
  ) as MappingContext[];
  const isMultiContext =
    contexts.length > 1 ||
    (contexts.length === 1 && contexts[0]?.source_kind !== "table");
  /**
   * La hoja de una tabla única. El parser la genera como `"table"`, pero se lee
   * del summary y no se hardcodea: mandar decisiones contra una hoja que el
   * archivo no tiene da 422 ("apunta a una hoja que no está en el archivo").
   *
   * `undefined` en summaries viejos sin `mapping_contexts`: ahí no hay hoja a la
   * cual referirse y el envío sigue siendo plano, como hasta ahora.
   */
  const hojaPlana = contexts[0]?.context_id;

  const { data: catalog, isLoading: loadingCatalog } = useFieldCatalog();

  const { data: suggestions = [], isLoading: loadingSuggestions } = useQuery({
    queryKey: ["column-mappings", fileId, entityType],
    queryFn: () => ingestionService.getColumnMappings(fileId, entityType),
    // Esperar al preview para conocer entityType; no correr en multi-hoja (cada
    // sección maneja su propia query) ni hasta que se elija propósito si es ambiguo.
    enabled: !!fileId && !!preview && !isMultiContext && !needsPurpose,
  });

  // Inicializar mappings desde sugerencias cuando cargan
  if (suggestions.length > 0 && !initialized) {
    const initial: Record<string, string> = {};
    for (const s of suggestions) {
      if (s.target_field) initial[s.source_column] = s.target_field;
    }
    setMappings(initial);
    setInitialized(true);
  }

  const { registrar: registrarImpacto, vista: vistaImpacto } =
    useImpactoPostConfirm(onDone, fileId);

  const confirmMutation = useMutation({
    mutationFn: (columnMappings: ColumnMapping[]) =>
      ingestionService.confirmFile(
        fileId,
        confirmedFields,
        columnMappings,
        undefined,
        undefined,
        hasStock ? stockTreatment : undefined,
        riskDecisions,
        hojaEfecto && efectoActual
          ? { [hojaEfecto.context_id]: efectoActual }
          : undefined,
        undefined,
        // Nunca. El importador plano no aplica las decisiones de costo, y el
        // confirm rechaza el archivo con 422 en cuanto ve una: mandar algo acá
        // sólo puede terminar en un rechazo. Ver el comentario de
        // `columnasDeCostoEnTablaUnica`.
        undefined,
      ),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      void queryClient.invalidateQueries({ queryKey: ["column-mappings-learned"] });
      // Avisos human-in-the-loop: el panel se cierra en onDone(), así que van como
      // toasts (compras sin proveedor/producto, filas a "Otros").
      for (const w of result.warnings ?? []) toast(w, "warning");
      // F-H3.c: con impacto sobre el inventario el panel queda abierto para
      // mostrarlo; sin impacto se cierra como siempre.
      if (registrarImpacto(result)) return;
      onDone();
    },
    onError: (error) => {
      // 409 (concurrente/ya importado) o timeout (import en curso): no es un error
      // de mapeo — se avisa amable y se refresca la lista, sin banner rojo.
      handleTransientConfirmError(error, toast, () =>
        queryClient.invalidateQueries({ queryKey: ["ingestion-files"] }),
      );
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => ingestionService.cancelFile(fileId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      onDone();
    },
  });

  function getMappingForColumn(col: string): string {
    return mappings[col] ?? "";
  }

  function setMappingForColumn(col: string, target: string) {
    // Cambio MANUAL del mapeo → marcar la columna como tocada (user_selected).
    touchedRef.current.add(riskKey("table", col));
    setMappings((prev) => ({ ...prev, [col]: target }));
  }

  // Calcula el status efectivo de una columna:
  // Si hay mapeo local → mapped/unmapped/ignore según el valor.
  // Si no hay mapeo local → usa el status del backend (captura required_missing).
  function computeEffectiveStatus(
    s: ColumnMappingSuggestion,
    currentTarget: string,
  ): ColumnMappingSuggestion["status"] {
    if (currentTarget === "ignore") return "unmapped";
    if (currentTarget) return "mapped";
    return s.status; // "unmapped" | "required_missing" del backend
  }

  function getUnmappedColumns(): string[] {
    return suggestions
      .filter((s) => !mappings[s.source_column] || mappings[s.source_column] === "")
      .map((s) => s.source_column);
  }

  // Se calcula abajo, contra `catalog[entityType].required`: un `custom_field:`
  // NO cubre un requerido, aunque el select lo muestre como mapeado.

  // A3: columnas que merecen revisión antes de confirmar — requeridas sin mapear,
  // sin mapear, ambiguas, o mapeadas con baja confianza (<50%). Las ignoradas a
  // propósito no. Dejar afuera las ambiguas era excluir de la lista de revisión
  // justo la columna sobre la que el backend dice que hace falta una decisión.
  const needsAttention = suggestions.filter((s) => {
    const t = getMappingForColumn(s.source_column);
    if (t === "ignore") return false;
    const eff = computeEffectiveStatus(s, t);
    if (eff === "required_missing" || eff === "unmapped" || eff === "ambiguo") return true;
    return eff === "mapped" && s.confidence < 0.5;
  });

  function handleConfirmClick() {
    const unmapped = getUnmappedColumns();
    if (unmapped.length > 0) {
      setUnmappedQueue(unmapped);
      setShowUnmappedModal(true);
    } else {
      doConfirm(mappings);
    }
  }

  function handleUnmappedResolve(target: string) {
    const current = unmappedQueue[0];
    const updatedMappings = current ? { ...mappings, [current]: target } : { ...mappings };
    if (current) {
      // El usuario asignó un target a esta columna → cambio manual.
      touchedRef.current.add(riskKey("table", current));
      setMappings(updatedMappings);
    }
    const remaining = unmappedQueue.slice(1);
    if (remaining.length > 0) {
      setUnmappedQueue(remaining);
    } else {
      setShowUnmappedModal(false);
      setUnmappedQueue([]);
      doConfirm(updatedMappings);
    }
  }

  function doConfirm(currentMappings: Record<string, string>) {
    const columnMappings: ColumnMapping[] = Object.entries(currentMappings)
      .filter(([, target]) => Boolean(target))
      .map(([src, target]) => ({
        source_column: src,
        target_field: target,
        // F8c: mismo touched-set que el recompute — user_selected solo si el
        // usuario cambió el mapeo a mano (no si vino de una sugerencia).
        user_selected: touchedRef.current.has(riskKey("table", src)),
        // F-H3.e: la columna dice a qué hoja pertenece. Sin esto el confirm
        // entra por la rama de mapeos planos, donde el efecto de inventario no
        // se puede resolver contra ninguna hoja y el backend lo rechaza con 422
        // (antes se descartaba en silencio: el usuario elegía reconstruir su
        // inventario y no pasaba nada). El resto del payload —las decisiones de
        // riesgo, el recálculo en vivo— ya viajaba calificado así.
        ...(hojaPlana ? { context_id: hojaPlana, entity_type: entityType } : {}),
      }));
    confirmMutation.mutate(columnMappings);
  }

  const rowCount =
    typeof summary?.rows_processed === "number" ? summary.rows_processed : null;
  const colCount = suggestions.length;
  const unmappedCount = getUnmappedColumns().length;
  const fields = catalog?.[entityType]?.fields ?? [];
  // Mismas dos reglas que en multi-hoja y que en el confirm del backend.
  const faltanRequeridos = missingRequiredFields(
    catalog?.[entityType]?.required ?? [],
    mappings,
    catalog?.[entityType]?.required_alternatives ?? {},
  );
  // Las dos ramas del mapeo colisionan igual: un escalar canónico y un campo
  // propio guardan un valor por fila, así que de dos columnas al mismo destino
  // sobrevive una sola. Se muestran juntas porque para el usuario son el mismo
  // problema y la salida es la misma.
  const colisiones = [
    ...scalarCollisions(fields, mappings),
    ...customFieldCollisions(mappings),
  ];

  // Preview rows para la tabla secundaria
  const previewRows = (
    (summary?.preview_rows as Record<string, unknown>[] | undefined) ??
    (summary?.ventas_detectadas as Record<string, unknown>[] | undefined) ??
    []
  ).slice(0, 10);

  const allHeaders = suggestions.map((s) => s.source_column);

  // Multi-hoja: delegar al mapeo por contexto (todos los hooks ya se ejecutaron).
  if (isMultiContext) {
    return (
      <MultiContextMapper
        fileId={fileId}
        contexts={contexts}
        contextualRisk={contextualRisk}
        masterPreviews={preview?.master_previews ?? []}
        parserWarnings={parserWarnings}
        onDone={onDone}
      />
    );
  }

  // F-H3.c: ídem para el archivo de un solo contexto.
  if (vistaImpacto) return vistaImpacto;

  return (
    <div className="ml-2 mt-3 rounded-xl border border-vk-border-w bg-vk-surface-w p-4">
      {/* Header */}
      <div className="mb-4 flex items-center justify-between gap-4">
        <div>
          <p className="text-sm font-semibold text-vk-text-primary">Mapeo de columnas</p>
          <p className="mt-0.5 text-xs text-vk-text-muted">
            {rowCount != null ? `${rowCount} filas · ` : ""}
            {colCount} columna{colCount !== 1 ? "s" : ""}
            {unmappedCount > 0 && (
              <span className="ml-1 font-medium text-vk-warning">
                · {unmappedCount} sin mapear
              </span>
            )}
          </p>
        </div>
        {/* Selector de tipo (ventas/gastos/productos/clientes/proveedores). En
            archivos ambiguos lo reemplaza el selector de propósito del bloque
            "Revisá antes de confirmar". */}
        <div className={`flex flex-wrap gap-2 ${isAmbiguous ? "hidden" : ""}`}>
          {(["ventas", "gastos", "productos", "clientes", "proveedores"] as const).map((key) => (
            <label key={key} className="flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={confirmedFields[key]}
                onChange={(e) => {
                  setConfirmedFields((prev) => ({ ...prev, [key]: e.target.checked }));
                  setInitialized(false);
                  setMappings({});
                  // F8c: mismo motivo que choosePurpose — el mapeo se re-deriva de
                  // las sugerencias del nuevo bucket, sin arrastrar touches viejos.
                  touchedRef.current.clear();
                }}
                className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
              />
              <span className="text-xs capitalize text-vk-text-secondary">{key}</span>
            </label>
          ))}
        </div>
      </div>

      {/* F7e: preview de maestros (clientes/proveedores) detectados en el archivo. */}
      <MasterPreviewPanel previews={preview?.master_previews ?? []} />

      {/* F8c: panel de decisiones de columnas riesgosas — en su propio contenedor
          (como en multi-hoja), NO dentro del wrapper "Revisá antes de confirmar":
          el panel ya trae su propio encabezado/caja, anidarlo duplicaba ambos. El
          riesgo se recalcula en vivo (recompute) al cambiar el mapeo/tipo del archivo. */}
      {contextualRisk.length > 0 && (
        <div className="mb-4">
          <ColumnRiskDecisionsPanel
            initialRisks={contextualRisk}
            recomputeKey={riskRecomputeKey}
            recompute={recomputeRisk}
            onDecisionsChange={setRiskDecisions}
            onCancelAndComplete={() => cancelMutation.mutate()}
          />
        </div>
      )}

      {/* A3: bloque consolidado "Revisá antes de confirmar" */}
      {(isAmbiguous || needsAttention.length > 0) && (
        <div className="mb-4 rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 p-3">
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-vk-text-primary">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 text-vk-warning" />
            Revisá antes de confirmar
          </p>

          {/* Selector de propósito cuando el tipo es ambiguo ("general") */}
          {isAmbiguous && (
            <div className="mb-3">
              <p className="mb-1 text-[11px] text-vk-text-secondary">
                No pudimos determinar si este archivo es de ventas, gastos o productos.
                Elegí el tipo:
              </p>
              <div className="flex flex-wrap gap-1.5">
                {PURPOSE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => choosePurpose(opt.value)}
                    className={`rounded-lg border px-3 py-1 text-xs transition-colors ${
                      purpose === opt.value
                        ? "border-vk-blue bg-vk-info-bg text-vk-blue"
                        : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Columnas que necesitan atención, editables inline */}
          {needsAttention.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] text-vk-text-secondary">
                Columnas a revisar ({needsAttention.length}):
              </p>
              <div className="space-y-1">
                {needsAttention.map((s) => {
                  const target = getMappingForColumn(s.source_column);
                  const eff = computeEffectiveStatus(s, target);
                  return (
                    <div key={s.source_column} className="flex items-center gap-2">
                      <StatusDot status={eff} />
                      <span
                        className="w-32 shrink-0 truncate font-mono text-[11px] text-vk-text-primary"
                        title={s.source_column}
                      >
                        {s.source_column}
                      </span>
                      <select
                        value={target}
                        onChange={(e) => setMappingForColumn(s.source_column, e.target.value)}
                        disabled={loadingCatalog}
                        className="min-w-0 flex-1 rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-[11px] text-vk-text-primary focus:border-vk-blue focus:outline-none disabled:opacity-50"
                      >
                        <option value="">Sin mapear</option>
                        <option value="ignore">— Ignorar —</option>
                        {fields.map((f) => (
                          <option key={f.value} value={f.value}>
                            {f.label}
                          </option>
                        ))}
                        {/* Sin esta opción el DOM cae a la primera y la pantalla
                            muestra "Sin mapear" sobre un target real. */}
                        {target &&
                          target !== "ignore" &&
                          fields.length > 0 &&
                          !fields.some((f) => f.value === target) && (
                            <option value={target}>
                              {target.startsWith("custom_field:")
                                ? target
                                : `${target} (campo desconocido)`}
                            </option>
                          )}
                      </select>
                      <AmbiguityHint
                        suggestion={s}
                        fields={fields}
                        onPick={(t) => setMappingForColumn(s.source_column, t)}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {needsPurpose ? (
        <div className="py-4 text-xs text-vk-text-muted">
          Elegí el tipo de archivo arriba para ver el mapeo de columnas.
        </div>
      ) : loadingSuggestions ? (
        <div className="flex items-center gap-2 py-4 text-xs text-vk-text-muted">
          <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
          Analizando columnas...
        </div>
      ) : (
        <>
          {/* Tabla de mapeo — dos paneles */}
          <div className="mb-4 overflow-hidden rounded-lg border border-vk-border-w">
            {/* Headers */}
            <div className="grid grid-cols-2 border-b border-vk-border-w bg-vk-bg-light px-3 py-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-vk-text-muted">
                Columna del archivo
              </span>
              <span className="text-xs font-semibold uppercase tracking-wide text-vk-text-muted">
                Campo en Véktor ({ENTITY_TYPE_LABELS[entityType] ?? entityType})
              </span>
            </div>

            {/* Filas de mapeo */}
            {suggestions.map((s, idx) => {
              const currentTarget = getMappingForColumn(s.source_column);
              const effectiveStatus = computeEffectiveStatus(s, currentTarget);
              const isMapped = effectiveStatus === "mapped";
              const isIgnored = currentTarget === "ignore";
              const isCustom = currentTarget.startsWith("custom_field:");

              return (
                <div
                  key={s.source_column}
                  className={`grid grid-cols-2 gap-0 border-b border-vk-border-w/60 last:border-0 ${
                    effectiveStatus === "required_missing" ? "bg-vk-danger/5" :
                    idx % 2 === 0 ? "bg-vk-surface-w" : "bg-vk-bg-light/40"
                  }`}
                >
                  {/* Panel izquierdo: columna del archivo */}
                  <div className="flex flex-col justify-center border-r border-vk-border-w/60 px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <StatusDot status={effectiveStatus} />
                      <span className="font-mono text-xs font-medium text-vk-text-primary">
                        {s.source_column}
                      </span>
                    </div>
                    {s.sample_values.length > 0 && (
                      <div className="mt-1 space-y-0.5 pl-4">
                        {s.sample_values.slice(0, 3).map((v, i) => (
                          <p key={i} className="truncate text-[11px] text-vk-text-muted">
                            {v}
                          </p>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Panel derecho: campo destino */}
                  <div className="flex flex-col justify-center px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      {(isMapped || isIgnored) && !isCustom && (
                        <ArrowRight className="h-3 w-3 shrink-0 text-vk-text-muted" />
                      )}
                      <select
                        value={currentTarget}
                        onChange={(e) => setMappingForColumn(s.source_column, e.target.value)}
                        className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-xs text-vk-text-primary focus:border-vk-blue focus:outline-none"
                      >
                        <option value="">Sin mapear</option>
                        <option value="ignore">— Ignorar columna —</option>
                        {fields.map((f) => (
                          <option key={f.value} value={f.value}>
                            {f.label}
                          </option>
                        ))}
                        {isCustom && (
                          <option value={currentTarget}>{currentTarget}</option>
                        )}
                      </select>
                    </div>
                    <div className="mt-1">
                      <AmbiguityHint
                        suggestion={s}
                        fields={fields}
                        onPick={(t) => setMappingForColumn(s.source_column, t)}
                      />
                    </div>
                    {isMapped && s.source !== "none" && (
                      <p className="mt-0.5 pl-5 text-[10px] text-vk-text-muted">
                        {SOURCE_LABELS[s.source] ?? s.source} ·{" "}
                        <span className={confidenceColor(s.confidence)}>
                          {Math.round(s.confidence * 100)}%
                        </span>
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Preview secundaria: todas las columnas, scroll horizontal */}
          {previewRows.length > 0 && allHeaders.length > 0 && (
            <details className="mb-4">
              <summary className="cursor-pointer text-xs text-vk-text-muted hover:text-vk-text-secondary">
                Ver preview de datos ({previewRows.length} filas)
              </summary>
              <div className="mt-2 overflow-x-auto rounded border border-vk-border-w">
                <table className="w-full whitespace-nowrap text-xs">
                  <thead>
                    <tr className="border-b border-vk-border-w bg-vk-bg-light">
                      {allHeaders.map((h) => (
                        <th
                          key={h}
                          className="px-2 py-1 text-left font-medium text-vk-text-muted"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {previewRows.map((row, i) => (
                      <tr key={i} className="border-b border-vk-border-w/60">
                        {allHeaders.map((h) => (
                          <td key={h} className="px-2 py-1 text-vk-text-secondary">
                            {String((row as Record<string, unknown>)[h] ?? "—")}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}

      {/* Requerido descubierto: se nombra el campo y la salida concreta. */}
      {faltanRequeridos.length > 0 && (
        <div className="mb-2 flex gap-2 rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-xs text-vk-danger">
          <XCircle className="mt-px h-3.5 w-3.5 shrink-0" />
          <div>
            <p>
              {faltanRequeridos.length === 1
                ? "Falta un dato obligatorio:"
                : "Faltan datos obligatorios:"}
            </p>
            {/* Mismo criterio que el banner del mapeo por hoja: se nombra el
                campo y se dice qué se pierde la persona si no lo mapea. Acá no
                hay a dónde saltar —la misma columna aparece en dos selects, el
                de "Revisá antes de confirmar" y el de la tabla—, y elegir uno
                de los dos sería elegir al azar. */}
            <ul className="mt-1 space-y-1">
              {explainMissing(faltanRequeridos, catalog?.[entityType]).map((f) => (
                <li key={f.field}>
                  <strong>{f.label}</strong>
                  {f.reason && (
                    <span className="mt-0.5 block text-[11px] leading-snug text-vk-text-secondary">
                      {f.reason}
                    </span>
                  )}
                </li>
              ))}
            </ul>
            <p className="mt-1">
              Elegí ese campo en la columna que lo contiene. Un campo personalizado
              guarda el dato pero no reemplaza al obligatorio.
            </p>
          </div>
        </div>
      )}

      {/* Colisión en un campo escalar: el importador se quedaba con la primera
          columna del orden del archivo y descartaba el resto en silencio. */}
      {colisiones.length > 0 && (
        <div className="mb-2 space-y-1 rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-xs text-vk-danger">
          {colisiones.map((c) => (
            <p key={c.target} className="flex gap-2">
              <XCircle className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>
                {c.columns.length} columnas apuntan a <strong>«{c.label}»</strong> (
                {c.columns.join(", ")}) y solo se puede guardar una. Elegí cuál
                corresponde y mandá las demás a otro campo o a «Ignorar».
              </span>
            </p>
          ))}
        </div>
      )}

      {/* A: tratamiento del stock cuando el archivo trae productos. */}
      {hasStock && !needsPurpose && (
        <StockTreatmentChoice
          value={stockTreatment}
          onChange={setStockTreatment}
          className="mb-3 rounded-lg border border-vk-border-w bg-vk-bg-light/40 p-3"
        />
      )}

      {/* F-H3.e: qué le hace al inventario esta tabla. Eje separado del de
          arriba: aquel decide si el stock inicial genera un gasto, éste qué
          pasa con las unidades. */}
      {hojaEfecto && !needsPurpose && (
        <InventoryEffectChoice
          hoja={hojaEfecto}
          value={efectoActual}
          onChange={setEffectElegido}
          className="mb-3 rounded-lg border border-vk-border-w bg-vk-bg-light/40 p-3"
        />
      )}

      {/* Un archivo de una sola tabla no puede traer costos de compra: el
          importador plano no cobra el envío y el confirm lo rechaza. Se dice
          acá, con las dos salidas, en vez de ofrecer tres ejes cuyo único
          desenlace posible es un 422. */}
      {!needsPurpose && columnasDeCostoEnTablaUnica.length > 0 && (
        <div className="mb-3 flex gap-2 rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-xs text-vk-danger">
          <XCircle className="mt-px h-3.5 w-3.5 shrink-0" />
          <div>
            <p>
              Este archivo es una sola tabla y trae{" "}
              {columnasDeCostoEnTablaUnica.length === 1 ? "una columna" : "columnas"}{" "}
              de envío (
              <span className="font-mono">
                {columnasDeCostoEnTablaUnica.join(", ")}
              </span>
              ). Véktor todavía no sabe cobrar ni repartir el envío en este
              formato: si lo importara, la compra quedaría con un costo más bajo
              que el real y el margen inflado.
            </p>
            <p className="mt-1 text-vk-text-secondary">
              Dos salidas: subilo como libro con hojas separadas (una por
              sección), o sacá esas columnas del mapeo —marcalas «Ignorar»— y
              cargá ese envío como un gasto aparte.
            </p>
          </div>
        </div>
      )}

      {/* Error de API (excluye 409/timeout: los maneja el toast, no el banner) */}
      {confirmMutation.isError &&
        !isTransientConfirmError(confirmMutation.error) && (
          <p className="mb-2 text-xs text-vk-danger">
            Error al confirmar. Verificá que los campos requeridos estén mapeados.
          </p>
        )}

      {/* Acciones */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handleConfirmClick}
          disabled={
            confirmMutation.isPending ||
            !Object.values(confirmedFields).some(Boolean) ||
            faltanRequeridos.length > 0 ||
            colisiones.length > 0 ||
            // El confirm lo va a rechazar igual: descubrirlo con un 422 después
            // de apretar es exactamente lo que este panel existe para evitar.
            columnasDeCostoEnTablaUnica.length > 0 ||
            needsPurpose
          }
          className="flex items-center gap-1.5 rounded-lg bg-vk-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
        >
          <CheckCircle className="h-3.5 w-3.5" />
          {confirmMutation.isPending
            ? "Confirmando..."
            : unmappedCount > 0
              ? `Confirmar (${unmappedCount} sin mapear)`
              : "Confirmar importación"}
        </button>
        <button
          type="button"
          onClick={() => cancelMutation.mutate()}
          disabled={cancelMutation.isPending || confirmMutation.isPending}
          className="rounded-lg border border-vk-border-w px-3 py-1.5 text-xs text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-50 transition-colors"
        >
          Cancelar
        </button>
      </div>

      {/* Modal para columnas sin mapear */}
      {showUnmappedModal && unmappedQueue.length > 0 && (
        <UnmappedModal
          column={unmappedQueue[0]!}
          entityType={entityType}
          onResolve={handleUnmappedResolve}
          onClose={() => {
            setShowUnmappedModal(false);
            setUnmappedQueue([]);
          }}
        />
      )}
    </div>
  );
}
