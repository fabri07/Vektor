"use client";

/**
 * Formulario público "Pedir acceso" — reemplaza al registro abierto.
 *
 * Es una sola página seccionada (contacto → rubro → tu negocio → tu info →
 * cómo querés usar Véktor), **no un wizard**: el visitante tiene que poder ver
 * de una cuánto le estamos pidiendo antes de empezar a contestar.
 *
 * **No pide contraseña.** Este formulario no crea una cuenta: manda una
 * solicitud que el dueño revisa a mano. La contraseña la define el usuario
 * recién cuando la solicitud se aprueba, con el link del mail de decisión.
 *
 * Anti-spam en capas, igual que `/contacto`: honeypot invisible (`website`) +
 * `elapsed_ms` medido desde el montaje. El rate limit por IP lo pone el backend.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AxiosError } from "axios";

import { useCreateAccessRequest } from "@/hooks/useAccessRequest";
import {
  isRequestedPlan,
  REQUESTED_PLAN_OPTIONS,
} from "@/lib/accessRequestOptions";
import { ctaSourceFromUrl, trackLandingEvent } from "@/lib/landingAnalytics";
import { REQUESTED_VERTICAL_OPTIONS, type RequestedVertical } from "@/lib/verticals";
import { buildAccessRequestPayload } from "@/services/accessRequest.service";
import {
  EMPTY_ACCESS_REQUEST_DRAFT,
  fieldErrors,
  parseAccessRequestDraft,
  type AccessRequestDraft,
} from "@/validation/accessRequest";
import {
  BusinessScreeningFields,
  Field,
  RadioGroup,
  inputClass,
} from "./BusinessScreeningFields";

export function AccessRequestForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const montadoEn = useMemo(() => Date.now(), []);
  const websiteRef = useRef<HTMLInputElement>(null); // honeypot
  const crear = useCreateAccessRequest();

  const [draft, setDraft] = useState<AccessRequestDraft>(EMPTY_ACCESS_REQUEST_DRAFT);
  const [enviado, setEnviado] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");

  // `?plan=` precarga la intención de plan pero la deja EDITABLE: el visitante
  // llegó desde el card de /precios, no firmó nada.
  const planDeUrl = searchParams.get("plan");
  useEffect(() => {
    if (isRequestedPlan(planDeUrl)) {
      setDraft((d) => (d.requested_plan ? d : { ...d, requested_plan: planDeUrl }));
    }
  }, [planDeUrl]);

  useEffect(() => {
    trackLandingEvent("access_request_form_view", {
      cta_source: ctaSourceFromUrl("solicitar_acceso"),
    });
  }, []);

  function update<K extends keyof AccessRequestDraft>(
    key: K,
    value: AccessRequestDraft[K],
  ) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  /**
   * Cambiar de rubro limpia el texto libre: con un rubro soportado el backend
   * rechaza `vertical_other_text`, y dejarlo cargado convertiría un cambio de
   * opinión en un 422.
   */
  function seleccionarRubro(code: RequestedVertical) {
    setDraft((d) => ({
      ...d,
      requested_vertical: code,
      vertical_other_text: code === "otros" ? d.vertical_other_text : "",
    }));
  }

  const parse = parseAccessRequestDraft(draft);
  const errores = fieldErrors(parse);
  const enviando = crear.isPending || enviado;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (enviando) return; // anti doble-submit
    if (!parse.success) return; // el botón ya está deshabilitado; guard defensivo

    const ctaSource = ctaSourceFromUrl("solicitar_acceso");
    setErrorMsg("");
    try {
      await crear.mutateAsync(
        buildAccessRequestPayload(parse.data, {
          ctaSource,
          website: websiteRef.current?.value ?? "",
          elapsedMs: Date.now() - montadoEn,
        }),
      );
      setEnviado(true);
      trackLandingEvent("access_request_submit_success", { cta_source: ctaSource });
      router.push(`/solicitud-enviada?email=${encodeURIComponent(parse.data.email)}`);
    } catch (err) {
      const httpStatus = err instanceof AxiosError ? err.response?.status : undefined;
      trackLandingEvent("access_request_submit_error", {
        cta_source: ctaSource,
        http_status: httpStatus,
      });
      setErrorMsg(
        httpStatus === 429
          ? "Recibimos varias solicitudes desde tu conexión. Probá de nuevo en un rato."
          : httpStatus === 422
            ? "Revisá los datos (por ejemplo el teléfono) e intentá de nuevo."
            : "No pudimos enviar tu solicitud. Probá de nuevo en un momento.",
      );
    }
  }

  return (
    <form onSubmit={onSubmit} className="mx-auto max-w-2xl px-6 pb-24">
      <div className="vektor-card space-y-10 p-6 sm:p-8">
        {/* Honeypot: oculto para humanos, tentador para bots. */}
        <input
          ref={websiteRef}
          type="text"
          name="website"
          tabIndex={-1}
          autoComplete="off"
          aria-hidden
          className="hidden"
        />

        {/* ── Contacto ─────────────────────────────────────────────────────── */}
        <section className="space-y-5">
          <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
            Contacto
          </h2>

          <Field label="Nombre y apellido" required error={errores.full_name}>
            <input
              className={inputClass}
              maxLength={200}
              value={draft.full_name}
              onChange={(e) => update("full_name", e.target.value)}
            />
          </Field>

          <Field label="Email" required error={errores.email}>
            <input
              type="email"
              className={inputClass}
              maxLength={255}
              value={draft.email}
              onChange={(e) => update("email", e.target.value)}
            />
          </Field>

          <Field
            label="Teléfono / WhatsApp (opcional)"
            hint="Si nos lo dejás, te escribimos por acá."
            error={errores.phone}
          >
            <input
              className={inputClass}
              maxLength={50}
              placeholder="+54 9 11 1234 5678"
              value={draft.phone}
              onChange={(e) => update("phone", e.target.value)}
            />
          </Field>

          <Field label="Nombre del negocio" required error={errores.business_name}>
            <input
              className={inputClass}
              maxLength={200}
              value={draft.business_name}
              onChange={(e) => update("business_name", e.target.value)}
            />
          </Field>
        </section>

        {/* ── Rubro ────────────────────────────────────────────────────────── */}
        <section className="space-y-5">
          <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
            Rubro
          </h2>

          <fieldset>
            <legend className="mb-3 block text-sm font-medium text-vektor-body">
              ¿De qué es tu negocio? <span className="text-vektor-red">*</span>
            </legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {REQUESTED_VERTICAL_OPTIONS.map((opcion) => {
                const seleccionado = draft.requested_vertical === opcion.code;
                return (
                  <button
                    key={opcion.code}
                    type="button"
                    aria-pressed={seleccionado}
                    onClick={() => seleccionarRubro(opcion.code)}
                    className={[
                      "flex flex-col items-start gap-3 rounded-xl border-2 p-4 text-left transition-all duration-150",
                      seleccionado
                        ? "border-vektor-blue bg-vektor-surface"
                        : "border-vektor-border hover:border-vektor-blue/50",
                    ].join(" ")}
                  >
                    <span
                      className={[
                        "inline-flex h-11 w-11 items-center justify-center rounded-xl transition-colors",
                        seleccionado
                          ? "bg-vektor-blue text-white"
                          : "bg-vektor-surface text-vektor-muted",
                      ].join(" ")}
                    >
                      <opcion.Icon />
                    </span>
                    <span>
                      <span className="block font-semibold text-vektor-white">
                        {opcion.name}
                      </span>
                      <span className="mt-1 block text-sm leading-snug text-vektor-muted">
                        {opcion.description}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </fieldset>

          {draft.requested_vertical === "otros" && (
            <Field
              label="Contanos de qué es tu negocio"
              required
              error={errores.vertical_other_text}
            >
              <textarea
                className={`${inputClass} min-h-[90px] resize-y`}
                maxLength={2000}
                value={draft.vertical_other_text}
                onChange={(e) => update("vertical_other_text", e.target.value)}
              />
            </Field>
          )}
        </section>

        <BusinessScreeningFields draft={draft} update={update} />

        {/* ── Cómo querés usar Véktor ──────────────────────────────────────── */}
        <section className="space-y-5">
          <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
            Cómo querés usar Véktor
          </h2>

          <RadioGroup
            name="requested_plan"
            legend="¿Con qué cuenta te gustaría comenzar?"
            options={REQUESTED_PLAN_OPTIONS}
            value={draft.requested_plan}
            onChange={(v) => update("requested_plan", v)}
            columns={1}
          />
        </section>

        {/* ── Consentimiento ───────────────────────────────────────────────── */}
        <div className="space-y-5">
          <label className="flex items-start gap-3 text-sm text-vektor-body">
            <input
              type="checkbox"
              className="mt-1 accent-vektor-blue"
              checked={draft.consent}
              onChange={(e) => update("consent", e.target.checked)}
            />
            <span>
              Acepto la{" "}
              <Link href="/privacidad" className="text-vektor-blue hover:underline">
                política de privacidad
              </Link>{" "}
              y el tratamiento de mis datos para que revisen mi solicitud.
            </span>
          </label>

          {errorMsg && (
            <p className="text-sm text-vektor-red" role="alert">
              {errorMsg}
            </p>
          )}

          <button
            type="submit"
            disabled={!parse.success || enviando}
            className="inline-flex w-full items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-6 py-3.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {enviando ? "Enviando..." : "Pedir acceso"}
          </button>

          <p className="text-center text-xs text-vektor-muted">
            ¿Ya tenés cuenta?{" "}
            <Link href="/login" className="text-vektor-blue hover:underline">
              Iniciá sesión
            </Link>
          </p>
        </div>
      </div>
    </form>
  );
}
