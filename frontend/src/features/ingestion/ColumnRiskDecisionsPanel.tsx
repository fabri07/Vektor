"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowRightCircle, Loader2, Trash2, XCircle } from "lucide-react";
import type {
  ColumnRiskDecision,
  ContextualColumnRisk,
} from "@/services/ingestion.service";

// F8c (Task 3a): panel de DECISIONES sobre columnas riesgosas dentro de un
// contexto. Reemplaza al legacy ColumnsAtRiskList (read-only, con bug de %).
// Se construye en aislamiento: el recompute lo inyecta el padre (Task 3b) ya
// cerrado sobre fileId + body; el panel solo lo invoca con un AbortSignal.

const RECOMPUTE_DEBOUNCE_MS = 400;

export interface ColumnRiskDecisionsPanelProps {
  // Riesgo inicial (viene del preview: preview.contextual_column_risk ?? []).
  initialRisks: ContextualColumnRisk[];
  // Clave serializada del mapeo/inclusión EFECTIVOS del usuario. Cuando cambia,
  // el panel recalcula el riesgo (debounced). La arma el padre (Task 3b).
  recomputeKey: string;
  // Callback que llama al backend con el input efectivo actual. El panel lo
  // invoca con un AbortSignal para poder cancelar respuestas viejas.
  recompute: (signal: AbortSignal) => Promise<ContextualColumnRisk[]>;
  // El panel reporta las decisiones actuales al padre (para el confirm).
  onDecisionsChange: (decisions: ColumnRiskDecision[]) => void;
  // Acción global "Cancelar y completar datos" (el padre llama a cancelFile).
  onCancelAndComplete: () => void;
}

type RiskAction = ColumnRiskDecision["action"];

const ROUTE_ACTION: RiskAction = "route_affected_rows_to_others";
const DROP_ACTION: RiskAction = "drop_column";

// Identidad de una columna riesgosa / decisión dentro del archivo: (contexto, columna).
// Serializamos la tupla con JSON.stringify en vez de concatenar con un separador:
// así no hay ambigüedad de caracteres ni colisión posible con nombres de columna reales.
function riskKey(contextId: string, sourceColumn: string): string {
  return JSON.stringify([contextId, sourceColumn]);
}

function requirementLabel(
  requirement: ContextualColumnRisk["field_requirement"],
): string {
  switch (requirement) {
    case "required":
      return "Requerido";
    case "explicitly_selected":
      return "Seleccionado";
    default:
      return "Opcional";
  }
}

// Un error de abort (fetch AbortError o axios ERR_CANCELED) no es un error real:
// significa que llegó una request más nueva y descartamos la vieja.
function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const anyErr = err as { name?: string; code?: string };
  return anyErr.name === "AbortError" || anyErr.code === "ERR_CANCELED";
}

// Ordena estable por (context_id, source_column) sin mutar el array de origen.
function sortRisks(risks: ContextualColumnRisk[]): ContextualColumnRisk[] {
  return [...risks].sort((a, b) => {
    if (a.context_id !== b.context_id) {
      return a.context_id < b.context_id ? -1 : 1;
    }
    if (a.source_column !== b.source_column) {
      return a.source_column < b.source_column ? -1 : 1;
    }
    return 0;
  });
}

// Poda: descarta toda decisión que ya no aplique al riesgo vigente de su columna.
function pruneDecisions(
  decisions: ColumnRiskDecision[],
  risks: ContextualColumnRisk[],
): ColumnRiskDecision[] {
  return decisions.filter((d) => {
    const risk = risks.find(
      (r) => r.context_id === d.context_id && r.source_column === d.source_column,
    );
    if (!risk) return false; // la columna ya no es riesgosa
    if (risk.target_field !== d.target_field) return false; // cambió el mapeo
    if (!risk.allowed_actions.includes(d.action)) return false; // acción ya no permitida
    return true;
  });
}

export function ColumnRiskDecisionsPanel({
  initialRisks,
  recomputeKey,
  recompute,
  onDecisionsChange,
  onCancelAndComplete,
}: ColumnRiskDecisionsPanelProps) {
  const [risks, setRisks] = useState<ContextualColumnRisk[]>(initialRisks);
  const [decisions, setDecisions] = useState<ColumnRiskDecision[]>([]);
  const [recomputing, setRecomputing] = useState(false);

  // Refs para mantener los effects estables (no dependen de callbacks del padre
  // que pueden ser inestables entre renders).
  const onDecisionsChangeRef = useRef(onDecisionsChange);
  const recomputeRef = useRef(recompute);
  // decisionsRef se mantiene sincronizado a mano en cada setter de `decisions`
  // (ver commitAction y la poda) para que los closures de los effects lean
  // siempre el valor más reciente sin tener que re-suscribirse a `decisions`.
  const decisionsRef = useRef(decisions);
  useEffect(() => {
    onDecisionsChangeRef.current = onDecisionsChange;
    recomputeRef.current = recompute;
  });

  // Guard de stale: token de secuencia + AbortController actual.
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);
  // Evita el doble-fetch inicial: sembramos de initialRisks y solo recalculamos
  // cuando recomputeKey CAMBIA respecto del montaje.
  const firstRunRef = useRef(true);
  // Evita aplicar el resultado de un recompute en vuelo si el componente ya se
  // desmontó (el AbortController cubre la request en sí, pero una promesa que
  // ya estaba resuelta/en curso al desmontar podría llegar a los .then/.catch
  // igual).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Recompute con debounce + guard de stale/abort.
  useEffect(() => {
    if (firstRunRef.current) {
      firstRunRef.current = false;
      return;
    }

    const timer = setTimeout(() => {
      // Abortar la request anterior (si sigue en vuelo) y arrancar una nueva.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const seq = seqRef.current + 1;
      seqRef.current = seq;
      setRecomputing(true);

      recomputeRef
        .current(controller.signal)
        .then((fresh) => {
          // Ignorar respuestas stale, abortadas, o si el componente se desmontó.
          if (!mountedRef.current || controller.signal.aborted || seq !== seqRef.current) return;
          setRisks(fresh);
        })
        .catch((err) => {
          if (!mountedRef.current || controller.signal.aborted || isAbortError(err)) return; // abort silencioso
          // Fail-soft: log suave y conservamos los risks previos.
          // eslint-disable-next-line no-console
          console.warn("[ColumnRiskDecisionsPanel] recompute falló", err);
        })
        .finally(() => {
          if (mountedRef.current && seq === seqRef.current) setRecomputing(false);
        });
    }, RECOMPUTE_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [recomputeKey]);

  // Poda de decisiones cada vez que cambian los risks (recompute).
  useEffect(() => {
    const prev = decisionsRef.current;
    const pruned = pruneDecisions(prev, risks);
    if (pruned.length !== prev.length) {
      decisionsRef.current = pruned;
      setDecisions(pruned);
      onDecisionsChangeRef.current(pruned);
    }
  }, [risks]);

  // Aplica/togglea una decisión con exclusión mutua drop/route por columna.
  function commitAction(risk: ContextualColumnRisk, action: RiskAction) {
    const key = riskKey(risk.context_id, risk.source_column);
    const prev = decisionsRef.current;
    const existing = prev.find(
      (d) => riskKey(d.context_id, d.source_column) === key,
    );

    let next: ColumnRiskDecision[];
    if (existing && existing.action === action) {
      // Click en la acción ya activa → togglea (quita la decisión).
      next = prev.filter((d) => riskKey(d.context_id, d.source_column) !== key);
    } else {
      // Reemplaza cualquier decisión previa de esta columna (exclusión mutua).
      next = [
        ...prev.filter((d) => riskKey(d.context_id, d.source_column) !== key),
        {
          context_id: risk.context_id,
          source_column: risk.source_column,
          target_field: risk.target_field,
          action,
        },
      ];
    }

    decisionsRef.current = next;
    setDecisions(next);
    onDecisionsChange(next);
  }

  if (risks.length === 0) return null;

  const sorted = sortRisks(risks);

  function activeActionFor(risk: ContextualColumnRisk): RiskAction | null {
    const key = riskKey(risk.context_id, risk.source_column);
    const found = decisions.find(
      (d) => riskKey(d.context_id, d.source_column) === key,
    );
    return found ? found.action : null;
  }

  return (
    <div
      className="rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 p-3"
      data-testid="column-risk-decisions-panel"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs font-semibold text-vk-text-primary">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 text-vk-warning" />
          Revisá antes de confirmar
        </p>
        {recomputing && (
          <span
            className="flex items-center gap-1 text-[11px] text-vk-text-muted"
            data-testid="column-risk-recomputing"
          >
            <Loader2 className="h-3 w-3 animate-spin" />
            Recalculando…
          </span>
        )}
      </div>

      <p className="mb-2 text-[11px] text-vk-text-secondary">
        Columnas con muchos datos vacíos:
      </p>

      <ul className="space-y-2">
        {sorted.map((risk) => {
          const active = activeActionFor(risk);
          const isDropped = active === DROP_ACTION;
          const isRouted = active === ROUTE_ACTION;
          const canRoute = risk.allowed_actions.includes(ROUTE_ACTION);
          const canDrop = risk.allowed_actions.includes(DROP_ACTION);
          const pct = Math.round(risk.null_ratio * 100);

          return (
            <li
              key={riskKey(risk.context_id, risk.source_column)}
              className="rounded-md border border-vk-border-w/60 bg-vk-surface-w/40 p-2"
              data-testid="column-risk-row"
            >
              <div className="flex items-start gap-1.5 text-[11px] text-vk-text-muted">
                <XCircle className="mt-0.5 h-3 w-3 shrink-0 text-vk-warning" />
                <div className="min-w-0 flex-1">
                  <span
                    className={
                      isDropped
                        ? "font-mono text-vk-text-muted line-through"
                        : "font-mono text-vk-text-secondary"
                    }
                  >
                    {risk.source_column}
                  </span>
                  <span className="ml-1.5 rounded bg-vk-warning-bg px-1 py-0.5 text-[10px] text-vk-text-secondary">
                    {requirementLabel(risk.field_requirement)}
                  </span>
                  <div className="mt-0.5">
                    {pct}% vacío
                    {risk.invalid_rows > 0
                      ? ` · ${risk.invalid_rows} valor(es) inválido(s)`
                      : ""}{" "}
                    · {risk.affected_rows} fila(s) afectada(s)
                  </div>
                  {risk.recommendation && (
                    <div className="mt-0.5 text-vk-text-muted">
                      {risk.recommendation}
                    </div>
                  )}
                  {isDropped && (
                    <div
                      className="mt-1 flex items-center gap-1 text-[11px] font-semibold text-vk-danger"
                      data-testid="column-risk-drop-badge"
                    >
                      <Trash2 className="h-3 w-3 shrink-0" />
                      Se eliminará al confirmar
                    </div>
                  )}
                </div>
              </div>

              {(canRoute || canDrop) && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {/* Con drop elegido NO ofrecemos más acciones sobre la columna. */}
                  {canRoute && !isDropped && (
                    <button
                      type="button"
                      onClick={() => commitAction(risk, ROUTE_ACTION)}
                      aria-pressed={isRouted}
                      className={`inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                        isRouted
                          ? "bg-vk-warning text-vk-bg-dark"
                          : "border border-vk-border-w text-vk-text-secondary hover:bg-vk-warning-bg"
                      }`}
                    >
                      <ArrowRightCircle className="h-3 w-3 shrink-0" />
                      Enviar filas afectadas a Otros
                    </button>
                  )}
                  {canDrop && (
                    <button
                      type="button"
                      onClick={() => commitAction(risk, DROP_ACTION)}
                      aria-pressed={isDropped}
                      className={`inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                        isDropped
                          ? "bg-vk-danger text-white"
                          : "border border-vk-border-w text-vk-text-secondary hover:bg-vk-danger-bg"
                      }`}
                    >
                      <Trash2 className="h-3 w-3 shrink-0" />
                      Eliminar columna
                    </button>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>

      <div className="mt-3 border-t border-vk-border-w/50 pt-2">
        <button
          type="button"
          onClick={onCancelAndComplete}
          className="text-[11px] font-medium text-vk-text-secondary underline-offset-2 hover:text-vk-text-primary hover:underline"
        >
          Cancelar y completar datos
        </button>
      </div>
    </div>
  );
}
