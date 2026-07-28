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

/**
 * Campo con etiqueta y, si corresponde, hint y mensaje de error.
 *
 * La etiqueta es **explícita** (`htmlFor` → `id`), no un `<label>` que envuelve
 * todo. Envolviendo, el hint y el error quedan dentro del nombre accesible y un
 * lector de pantalla anuncia el campo como *"Nombre y apellido * Escribí tu
 * nombre y apellido"*: el error se lee como parte de la etiqueta y el campo
 * nunca se anuncia como inválido. Acá el nombre es solo la etiqueta, y hint y
 * error se asocian como DESCRIPCIÓN vía `aria-describedby` (ver `fieldAria`).
 */
export function Field({
  campo,
  label,
  required,
  hint,
  error,
  children,
}: {
  /** Clave del borrador. Deriva el `id` del control y los de hint/error. */
  campo: string;
  label: string;
  required?: boolean;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  const id = fieldAnchorId(campo);
  return (
    <div className="block">
      <label htmlFor={id} className="mb-2 block text-sm font-medium text-vektor-body">
        {label} {required && <span className="text-vektor-red">*</span>}
      </label>
      {hint && (
        <p id={`${id}-hint`} className="mb-2 block text-xs text-vektor-muted">
          {hint}
        </p>
      )}
      {children}
      {error && (
        <p id={`${id}-error`} className="mt-1.5 block text-xs text-vektor-red" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * `id` del primer control de un campo. Lo usa el resumen de faltantes para
 * llevar el foco al campo que quedó sin contestar.
 */
export function fieldAnchorId(field: string): string {
  return `campo-${field}`;
}

/**
 * Atributos que el control de un `Field` tiene que llevar: el `id` con el que
 * la etiqueta lo nombra, `aria-invalid` cuando hay error, y la asociación de
 * hint y error como descripción.
 *
 * Va en el caller y no dentro de `Field` (que tendría que clonar el hijo)
 * porque el control es distinto en cada campo — input, textarea — y clonar a
 * ciegas es la clase de magia que rompe callado cuando alguien anida un `div`.
 */
export function fieldAria(
  campo: string,
  { error, hint }: { error?: string; hint?: string } = {},
) {
  const id = fieldAnchorId(campo);
  const descrito = [hint ? `${id}-hint` : null, error ? `${id}-error` : null]
    .filter(Boolean)
    .join(" ");
  return {
    id,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": descrito || undefined,
  };
}

/**
 * Grupo de radios de opción cerrada. Sin preselección: nada arranca marcado.
 *
 * Muestra su propio error: con 12 campos requeridos repartidos en cinco
 * secciones, un grupo mudo deja al usuario con el botón gris y sin idea de qué
 * le falta.
 */
export function RadioGroup<T extends string>({
  name,
  legend,
  options,
  value,
  onChange,
  columns = 2,
  error,
}: {
  name: string;
  legend: string;
  options: readonly Choice<T>[];
  value: string;
  onChange: (value: T) => void;
  columns?: 1 | 2 | 3;
  error?: string;
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
        {options.map((opt, indice) => (
          <label
            key={opt.value}
            className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
              value === opt.value
                ? "border-vektor-blue text-vektor-white"
                : error
                  ? "border-vektor-red/60 text-vektor-muted hover:border-vektor-blue/50"
                  : "border-vektor-border text-vektor-muted hover:border-vektor-blue/50"
            }`}
          >
            <input
              // Solo el primero lleva ancla: es el destino del foco.
              id={indice === 0 ? fieldAnchorId(name) : undefined}
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
      {error && (
        <p className="mt-1.5 text-xs text-vektor-red" role="alert">
          {error}
        </p>
      )}
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
  /**
   * Errores YA filtrados por el contenedor: acá solo llegan los que
   * corresponde mostrar (campo tocado o intento de envío). Este componente no
   * decide cuándo mostrarlos, solo los pinta.
   */
  errores: Record<string, string>;
}

export function BusinessScreeningFields({
  draft,
  update,
  errores,
}: BusinessScreeningFieldsProps) {
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
          error={errores.years_operating}
        />

        <RadioGroup
          name="staff_size"
          legend="¿Cuánta gente trabaja?"
          options={STAFF_SIZE_OPTIONS}
          value={draft.staff_size}
          onChange={(v) => update("staff_size", v)}
          error={errores.staff_size}
        />

        <RadioGroup
          name="main_concern"
          legend="¿Qué te preocupa más?"
          options={MAIN_CONCERN_OPTIONS}
          value={draft.main_concern}
          onChange={(v) => update("main_concern", v)}
          error={errores.main_concern}
          columns={3}
        />

        <ConfidentialityNotice />

        <RadioGroup
          name="monthly_revenue_band"
          legend="Facturación mensual aproximada"
          options={REVENUE_BAND_OPTIONS}
          value={draft.monthly_revenue_band}
          onChange={(v) => update("monthly_revenue_band", v)}
          error={errores.monthly_revenue_band}
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
          error={errores.records_format}
        />

        <RadioGroup
          name="history_depth"
          legend="¿Desde cuándo tenés esos registros?"
          options={HISTORY_DEPTH_OPTIONS}
          value={draft.history_depth}
          onChange={(v) => update("history_depth", v)}
          error={errores.history_depth}
        />

        <RadioGroup
          name="can_share_files"
          legend="¿Esos archivos los podrías subir para arrancar?"
          options={CAN_SHARE_FILES_OPTIONS}
          value={draft.can_share_files}
          onChange={(v) => update("can_share_files", v)}
          error={errores.can_share_files}
          columns={3}
        />

        <Field campo="records_notes" label="Contanos cómo lo llevás (opcional)">
          <textarea
            {...fieldAria("records_notes")}
            className={`${inputClass} min-h-[110px] resize-y`}
            maxLength={2000}
            placeholder={RECORDS_NOTES_PLACEHOLDER}
            value={draft.records_notes}
            onChange={(e) => update("records_notes", e.target.value)}
          />
        </Field>

        <Field campo="applicant_notes" label="Algo más que quieras contarnos (opcional)">
          <textarea
            {...fieldAria("applicant_notes")}
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
