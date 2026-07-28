"use client";

/**
 * /contacto — formulario público "Contactanos" (replica del de adhoc, en dark).
 *
 * Persiste el lead vía POST /contact/leads. Anti-spam: honeypot invisible
 * (`website`) + `elapsed_ms` (tiempo desde el render). Consentimiento de
 * privacidad obligatorio con enlace a /privacidad. Guard anti doble-submit.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AxiosError } from "axios";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/public/PageHeader";
import { CONSENT_VERSION } from "@/lib/contact";
import { ctaSourceFromUrl, trackLandingEvent } from "@/lib/landingAnalytics";
import { VERTICAL_OPTIONS } from "@/lib/verticals";

/**
 * Los tres rubros reales salen de `lib/verticals.ts`, la misma fuente que usa
 * el formulario de solicitud de acceso.
 *
 * ⚠️ El "Otro" NO se toma de ahí: este formulario postea a `/contact/leads`,
 * cuyo enum `LeadRubro` usa `"otro"` (singular), mientras que
 * `RequestedVertical` usa `"otros"`. Son dos endpoints con dos vocabularios
 * distintos; unificarlos acá del lado del cliente sería un 422 en cada lead que
 * elija "Otro".
 */
const RUBRO_OTRO_LEAD = { value: "otro", label: "Otro" };

const RUBROS = [
  ...VERTICAL_OPTIONS.map((o) => ({ value: o.code as string, label: o.name })),
  RUBRO_OTRO_LEAD,
];

const USUARIOS = [
  { value: "1_5", label: "1 a 5" },
  { value: "6_10", label: "6 a 10" },
  { value: "11_20", label: "11 a 20" },
  { value: "21_30", label: "21 a 30" },
  { value: "31_40", label: "31 a 40" },
  { value: "41_50", label: "41 a 50" },
  { value: "51_80", label: "51 a 80" },
  { value: "80_plus", label: "+ 80" },
];

const STEPS = [
  "Completá tus datos",
  "Te contactamos para entender tus necesidades",
  "Coordinamos una demo del sistema adaptado a tu negocio",
];

const inputClass =
  "w-full rounded-xl border border-vektor-border bg-vektor-ink px-4 py-3 text-vektor-white placeholder:text-vektor-muted focus:border-vektor-blue focus:outline-none";

export default function ContactoPage() {
  const renderedAt = useMemo(() => Date.now(), []);
  const websiteRef = useRef<HTMLInputElement>(null); // honeypot

  // Analytics: formulario visto (una vez). Origen real del CTA desde ?src=.
  useEffect(() => {
    trackLandingEvent("contact_form_view", {
      cta_source: ctaSourceFromUrl("contacto_page"),
    });
  }, []);

  const [form, setForm] = useState({
    nombre: "",
    celular: "+54 ",
    email: "",
    empresa: "",
    rubro: "",
    cantidad_usuarios: "",
    gestion_actual: "",
  });
  const [consent, setConsent] = useState(false);
  const [status, setStatus] = useState<"idle" | "sending" | "ok" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  function update<K extends keyof typeof form>(key: K, value: string) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (status === "sending") return; // anti doble-submit
    if (!consent) {
      setErrorMsg("Necesitás aceptar la política de privacidad.");
      setStatus("error");
      return;
    }
    setStatus("sending");
    setErrorMsg("");
    const ctaSource = ctaSourceFromUrl("contacto_page");
    try {
      await api.post("/contact/leads", {
        ...form,
        gestion_actual: form.gestion_actual || null,
        consent: true,
        consent_version: CONSENT_VERSION,
        cta_source: ctaSource,
        website: websiteRef.current?.value || "",
        elapsed_ms: Date.now() - renderedAt,
      });
      trackLandingEvent("contact_form_submit_success", { cta_source: ctaSource });
      setStatus("ok");
    } catch (err) {
      const httpStatus = err instanceof AxiosError ? err.response?.status : undefined;
      trackLandingEvent("contact_form_submit_error", {
        cta_source: ctaSource,
        http_status: httpStatus,
      });
      setErrorMsg(
        httpStatus === 422
          ? "Revisá los datos (por ejemplo el teléfono) e intentá de nuevo."
          : "No pudimos enviar el formulario. Probá de nuevo en un momento."
      );
      setStatus("error");
    }
  }

  if (status === "ok") {
    return (
      <div className="mx-auto max-w-2xl px-6 py-28 text-center">
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight text-vektor-white">
          ¡Gracias!
        </h1>
        <p className="mt-4 text-lg text-vektor-body">
          Recibimos tus datos. Te vamos a contactar a la brevedad para conversar.
        </p>
        <Link
          href="/"
          className="mt-8 inline-flex rounded-full border border-white/25 px-6 py-3 text-sm font-semibold text-vektor-white hover:border-white/50"
        >
          Volver al inicio
        </Link>
      </div>
    );
  }

  return (
    <div className="pb-24">
      <PageHeader
        title="Dejá tus datos y te contactaremos para conversar"
        subtitle="Contanos de tu negocio y coordinamos una demo sin compromiso."
      />

      {/* Pasos */}
      <div className="mx-auto mb-12 grid max-w-4xl gap-4 px-6 sm:grid-cols-3">
        {STEPS.map((step, i) => (
          <div key={i} className="vektor-card p-6 text-center">
            <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-vektor-surface font-display text-lg text-vektor-teal">
              {i + 1}
            </div>
            <p className="text-sm text-vektor-body">{step}</p>
          </div>
        ))}
      </div>

      <form onSubmit={onSubmit} className="mx-auto max-w-2xl px-6">
        <div className="vektor-card space-y-5 p-6 sm:p-8">
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

          <Field label="Tu nombre" required>
            <input
              className={inputClass}
              required
              maxLength={120}
              value={form.nombre}
              onChange={(e) => update("nombre", e.target.value)}
            />
          </Field>

          <Field label="Tu celular" required>
            <input
              className={inputClass}
              required
              maxLength={40}
              value={form.celular}
              onChange={(e) => update("celular", e.target.value)}
            />
          </Field>

          <Field label="Correo corporativo" required>
            <input
              type="email"
              className={inputClass}
              required
              maxLength={255}
              value={form.email}
              onChange={(e) => update("email", e.target.value)}
            />
          </Field>

          <Field label="Empresa" required>
            <input
              className={inputClass}
              required
              maxLength={160}
              value={form.empresa}
              onChange={(e) => update("empresa", e.target.value)}
            />
          </Field>

          <Field label="Rubro" required>
            <select
              className={inputClass}
              required
              value={form.rubro}
              onChange={(e) => update("rubro", e.target.value)}
            >
              <option value="" disabled>
                Elegí tu rubro
              </option>
              {RUBROS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </Field>

          <fieldset>
            <legend className="mb-2 block text-sm font-medium text-vektor-body">
              Usuarios (personas que van a usar el sistema){" "}
              <span className="text-vektor-red">*</span>
            </legend>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {USUARIOS.map((u) => (
                <label
                  key={u.value}
                  className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                    form.cantidad_usuarios === u.value
                      ? "border-vektor-blue text-vektor-white"
                      : "border-vektor-border text-vektor-muted"
                  }`}
                >
                  <input
                    type="radio"
                    name="cantidad_usuarios"
                    required
                    className="accent-vektor-blue"
                    value={u.value}
                    checked={form.cantidad_usuarios === u.value}
                    onChange={(e) => update("cantidad_usuarios", e.target.value)}
                  />
                  {u.label}
                </label>
              ))}
            </div>
          </fieldset>

          <Field label="¿Cómo gestionan la empresa actualmente?">
            <textarea
              className={`${inputClass} min-h-[110px] resize-y`}
              maxLength={2000}
              placeholder="Excel, sistema propio, papel, varios sistemas desconectados, etc..."
              value={form.gestion_actual}
              onChange={(e) => update("gestion_actual", e.target.value)}
            />
          </Field>

          <label className="flex items-start gap-3 text-sm text-vektor-body">
            <input
              type="checkbox"
              required
              className="mt-1 accent-vektor-blue"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            <span>
              Acepto la{" "}
              <Link href="/privacidad" className="text-vektor-blue hover:underline">
                política de privacidad
              </Link>{" "}
              y el tratamiento de mis datos para ser contactado.
            </span>
          </label>

          {status === "error" && (
            <p className="text-sm text-vektor-red" role="alert">
              {errorMsg}
            </p>
          )}

          <button
            type="submit"
            disabled={status === "sending"}
            className="inline-flex w-full items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal px-6 py-3.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-60"
          >
            {status === "sending" ? "Enviando..." : "Enviar"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-vektor-body">
        {label} {required && <span className="text-vektor-red">*</span>}
      </span>
      {children}
    </label>
  );
}
