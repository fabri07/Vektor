"use client";

/**
 * Bloques "TU NEGOCIO" y "TU INFO" del formulario de solicitud de acceso.
 *
 * Son las preguntas con las que el dueño decide si las heurísticas de los
 * rubros existentes le sirven a ese negocio. Todas son de opción cerrada
 * (catálogos en `lib/accessRequestOptions.ts`, espejo de los `StrEnum` del
 * backend) salvo los dos textos libres.
 *
 * El aviso de confidencialidad va **arriba** de la facturación mensual, no
 * abajo: si aparece después de la pregunta ya generó la desconfianza que venía
 * a desactivar.
 *
 * Exporta también las primitivas de campo (`Field`, `RadioGroup`, `inputClass`)
 * porque el formulario contenedor usa exactamente las mismas — duplicarlas
 * sería el camino directo a dos estilos de input en la misma página.
 */

import type { ReactNode } from "react";

import {
  CAN_SHARE_FILES_OPTIONS,
  HISTORY_DEPTH_OPTIONS,
  MAIN_CONCERN_OPTIONS,
  RECORDS_FORMAT_OPTIONS,
  RECORDS_NOTES_PLACEHOLDER,
  REVENUE_BAND_OPTIONS,
  STAFF_SIZE_OPTIONS,
  YEARS_OPERATING_OPTIONS,
  type Choice,
} from "@/lib/accessRequestOptions";
import { CONFIDENTIALITY_NOTICE } from "@/lib/privacyNotices";
import type { AccessRequestDraft } from "@/validation/accessRequest";

export const inputClass =
  "w-full rounded-xl border border-vektor-border bg-vektor-ink px-4 py-3 text-vektor-white placeholder:text-vektor-muted focus:border-vektor-blue focus:outline-none";

/** Campo con etiqueta y, si corresponde, el mensaje de error del schema. */
export function Field({
  label,
  required,
  hint,
  error,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-vektor-body">
        {label} {required && <span className="text-vektor-red">*</span>}
      </span>
      {hint && <span className="mb-2 block text-xs text-vektor-muted">{hint}</span>}
      {children}
      {error && (
        <span className="mt-1.5 block text-xs text-vektor-red" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

/** Grupo de radios de opción cerrada. Sin preselección: nada arranca marcado. */
export function RadioGroup<T extends string>({
  name,
  legend,
  options,
  value,
  onChange,
  columns = 2,
}: {
  name: string;
  legend: string;
  options: readonly Choice<T>[];
  value: string;
  onChange: (value: T) => void;
  columns?: 1 | 2 | 3;
}) {
  const grid =
    columns === 1
      ? "grid-cols-1"
      : columns === 3
        ? "grid-cols-1 sm:grid-cols-3"
        : "grid-cols-1 sm:grid-cols-2";
  return (
    <fieldset>
      <legend className="mb-2 block text-sm font-medium text-vektor-body">
        {legend} <span className="text-vektor-red">*</span>
      </legend>
      <div className={`grid gap-2 ${grid}`}>
        {options.map((opt) => (
          <label
            key={opt.value}
            className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
              value === opt.value
                ? "border-vektor-blue text-vektor-white"
                : "border-vektor-border text-vektor-muted hover:border-vektor-blue/50"
            }`}
          >
            <input
              type="radio"
              name={name}
              className="mt-0.5 accent-vektor-blue"
              value={opt.value}
              checked={value === opt.value}
              onChange={() => onChange(opt.value)}
            />
            <span>
              {opt.label}
              {opt.detail && (
                <span className="mt-0.5 block text-xs text-vektor-muted">{opt.detail}</span>
              )}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** Aviso de confidencialidad: texto fijo, sin variantes por contexto. */
export function ConfidentialityNotice() {
  return (
    <div className="rounded-xl border border-vektor-border bg-vektor-surface/60 p-4">
      <p className="mb-2 flex items-center gap-2 text-sm font-semibold text-vektor-white">
        <span aria-hidden>🔒</span>
        {CONFIDENTIALITY_NOTICE.title}
      </p>
      {CONFIDENTIALITY_NOTICE.paragraphs.map((parrafo) => (
        <p key={parrafo.slice(0, 24)} className="mt-2 text-xs leading-relaxed text-vektor-muted">
          {parrafo}
        </p>
      ))}
    </div>
  );
}

export interface BusinessScreeningFieldsProps {
  draft: AccessRequestDraft;
  update: <K extends keyof AccessRequestDraft>(
    key: K,
    value: AccessRequestDraft[K],
  ) => void;
}

export function BusinessScreeningFields({ draft, update }: BusinessScreeningFieldsProps) {
  return (
    <>
      <section className="space-y-5">
        <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
          Tu negocio
        </h2>

        <RadioGroup
          name="years_operating"
          legend="¿Hace cuánto opera?"
          options={YEARS_OPERATING_OPTIONS}
          value={draft.years_operating}
          onChange={(v) => update("years_operating", v)}
        />

        <RadioGroup
          name="staff_size"
          legend="¿Cuánta gente trabaja?"
          options={STAFF_SIZE_OPTIONS}
          value={draft.staff_size}
          onChange={(v) => update("staff_size", v)}
        />

        <RadioGroup
          name="main_concern"
          legend="¿Qué te preocupa más?"
          options={MAIN_CONCERN_OPTIONS}
          value={draft.main_concern}
          onChange={(v) => update("main_concern", v)}
          columns={3}
        />

        <ConfidentialityNotice />

        <RadioGroup
          name="monthly_revenue_band"
          legend="Facturación mensual aproximada"
          options={REVENUE_BAND_OPTIONS}
          value={draft.monthly_revenue_band}
          onChange={(v) => update("monthly_revenue_band", v)}
        />
      </section>

      <section className="space-y-5">
        <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
          Tu info
        </h2>

        <RadioGroup
          name="records_format"
          legend="¿Guardás registro de ventas y gastos?"
          options={RECORDS_FORMAT_OPTIONS}
          value={draft.records_format}
          onChange={(v) => update("records_format", v)}
        />

        <RadioGroup
          name="history_depth"
          legend="¿Desde cuándo tenés esos registros?"
          options={HISTORY_DEPTH_OPTIONS}
          value={draft.history_depth}
          onChange={(v) => update("history_depth", v)}
        />

        <RadioGroup
          name="can_share_files"
          legend="¿Esos archivos los podrías subir para arrancar?"
          options={CAN_SHARE_FILES_OPTIONS}
          value={draft.can_share_files}
          onChange={(v) => update("can_share_files", v)}
          columns={3}
        />

        <Field label="Contanos cómo lo llevás (opcional)">
          <textarea
            className={`${inputClass} min-h-[110px] resize-y`}
            maxLength={2000}
            placeholder={RECORDS_NOTES_PLACEHOLDER}
            value={draft.records_notes}
            onChange={(e) => update("records_notes", e.target.value)}
          />
        </Field>

        <Field label="Algo más que quieras contarnos (opcional)">
          <textarea
            className={`${inputClass} min-h-[90px] resize-y`}
            maxLength={2000}
            value={draft.applicant_notes}
            onChange={(e) => update("applicant_notes", e.target.value)}
          />
        </Field>
      </section>
    </>
  );
}
