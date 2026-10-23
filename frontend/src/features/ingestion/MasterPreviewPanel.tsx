"use client";

import { AlertCircle, Info } from "lucide-react";
import type { MasterPreviewSample, MasterPreviewSummary } from "@/services/ingestion.service";

// F7e: panel de preview de maestros (clientes/proveedores) — expone lo que F7d
// ya calcula en el backend (GET /files/{id}/preview → master_previews) ANTES
// de confirmar. Solo diagnóstico: nada de esto persiste.

const MASTER_ENTITY_LABELS: Record<string, string> = {
  customer: "Clientes",
  supplier: "Proveedores",
};

const SAMPLE_STATUS_LABELS: Record<string, string> = {
  needs_review: "Faltan datos para identificar",
  invalid: "Dato inválido",
  duplicate_in_file: "Duplicado dentro del archivo",
};

function MiniStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "success" | "info" | "warning" | "muted";
}) {
  const toneClass =
    tone === "success"
      ? "text-vk-success"
      : tone === "info"
        ? "text-vk-info"
        : tone === "warning"
          ? "text-vk-warning"
          : "text-vk-text-secondary";
  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-bg-light/40 px-2 py-1.5 text-center">
      <div className={`text-sm font-semibold ${toneClass}`}>{value}</div>
      <div className="mt-0.5 text-[10px] text-vk-text-muted">{label}</div>
    </div>
  );
}

// Solo needs_review/invalid/duplicate_in_file: son las filas que el confirm
// NO importa. No es un error — se comunica como "faltan datos", nunca como fallo.
function SampleRow({ sample }: { sample: MasterPreviewSample }) {
  const isInvalid = sample.status === "invalid";
  return (
    <li className="flex items-start gap-1.5 text-[11px] text-vk-text-muted">
      {isInvalid ? (
        <Info className="mt-0.5 h-3 w-3 shrink-0 text-vk-text-muted" />
      ) : (
        <AlertCircle className="mt-0.5 h-3 w-3 shrink-0 text-vk-warning" />
      )}
      <span>
        <span className="text-vk-text-secondary">
          {sample.display_name ?? `Fila ${sample.row_index + 1}`}
        </span>
        {" · "}
        {SAMPLE_STATUS_LABELS[sample.status] ?? sample.status}
        {sample.issue && ` — ${sample.issue}`}
        {sample.existing_name && ` (existente: ${sample.existing_name})`}
      </span>
    </li>
  );
}

function MasterPreviewCard({ preview }: { preview: MasterPreviewSummary }) {
  const reviewSamples = preview.samples.filter(
    (s) => s.status === "needs_review" || s.status === "invalid" || s.status === "duplicate_in_file",
  );
  const hasReview = preview.needs_review + preview.invalid + preview.duplicates > 0;

  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="text-xs font-semibold text-vk-text-primary">
          {MASTER_ENTITY_LABELS[preview.entity_type] ?? preview.entity_type}
        </p>
        <span className="text-[10px] text-vk-text-muted">Vista previa — no persiste nada</span>
      </div>

      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-5">
        <MiniStat label="Se crean" value={preview.to_create} tone="success" />
        <MiniStat label="Se actualizan" value={preview.to_update} tone="info" />
        <MiniStat label="En revisión" value={preview.needs_review} tone="warning" />
        <MiniStat label="Inválidos" value={preview.invalid} tone="muted" />
        <MiniStat label="Duplicados" value={preview.duplicates} tone="muted" />
      </div>

      {hasReview && (
        <p className="mt-2 text-[11px] text-vk-text-secondary">
          Solo se importan los registros a crear/actualizar. Los de revisión, inválidos
          y duplicados no se guardan — podés corregir el archivo y volver a leerlo.
        </p>
      )}

      {reviewSamples.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-vk-text-muted hover:text-vk-text-secondary">
            Ver por qué ({reviewSamples.length} de {preview.samples.length} filas de muestra)
          </summary>
          <ul className="mt-1.5 space-y-1 border-t border-vk-border-w/60 pt-1.5">
            {reviewSamples.map((s) => (
              <SampleRow key={s.row_index} sample={s} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function MasterPreviewPanel({ previews }: { previews: MasterPreviewSummary[] }) {
  if (previews.length === 0) return null;
  return (
    <div className="mb-4 space-y-2">
      {previews.map((p) => (
        <MasterPreviewCard key={`${p.entity_type}-${p.context_id ?? "single"}`} preview={p} />
      ))}
    </div>
  );
}
