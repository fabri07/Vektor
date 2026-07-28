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
 * **Alta por Google.** Con `?prefill=<token>` el formulario viene de "Continuar
 * con Google" con un email que Google ya verificó: se prellenan email y nombre,
 * el email queda de solo lectura (es lo que liga la solicitud a esa identidad)
 * y el token se devuelve en el POST para que el backend persista el
 * `google_subject`. La lectura del prefill NO consume el token — la única toma
 * es el POST.
 *
 * Anti-spam en capas, igual que `/contacto`: honeypot invisible (`website`) +
 * `elapsed_ms` medido desde el montaje. El rate limit por IP lo pone el backend.
 *
 * **Superficie de errores.** El formulario arranca en silencio: un campo solo
 * muestra su error después de que el usuario lo tocó (`onBlur` en los textos,
 * la selección en los grupos) o después de un intento de envío. Al revés,
 * ningún campo requerido se queda mudo: los 9 grupos de opción muestran el
 * suyo, y arriba del botón hay un resumen de lo que falta con foco directo a
 * cada campo. Las dos mitades son la misma regla — decir lo que falta, cuando
 * corresponde, y nada más.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AxiosError } from "axios";

import { useCreateAccessRequest } from "@/hooks/useAccessRequest";
import {
  ACCESS_REQUEST_FIELD_LABELS,
  accessRequestFieldLabel,
  isRequestedPlan,
  REQUESTED_PLAN_OPTIONS,
} from "@/lib/accessRequestOptions";
import { ctaSourceFromUrl, trackLandingEvent } from "@/lib/landingAnalytics";
import { REQUESTED_VERTICAL_OPTIONS, type RequestedVertical } from "@/lib/verticals";
import {
  buildAccessRequestPayload,
  fetchGooglePrefill,
} from "@/services/accessRequest.service";
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
  fieldAnchorId,
  fieldAria,
  inputClass,
} from "./BusinessScreeningFields";

/** Lleva el foco (y la vista) al primer control del campo indicado. */
function enfocarCampo(campo: string) {
  const control = document.getElementById(fieldAnchorId(campo));
  if (!control) return;
  control.focus({ preventScroll: true });
  control.scrollIntoView({ block: "center", behavior: "smooth" });
}

const HINT_TELEFONO = "Si nos lo dejás, te escribimos por acá.";
const HINT_EMAIL_GOOGLE = "Lo verificó Google. Es el email con el que vas a entrar.";

/**
 * Lo que se le dice al visitante cuando el prefill de Google ya no existe al
 * momento de enviar.
 *
 * Es la verdad completa y en el orden en el que le importa: qué dejó de valer,
 * qué NO se pierde, y cómo va a entrar. No menciona "token" ni "sesión
 * expirada" a secas — el visitante no tiene contexto para traducir eso a una
 * consecuencia.
 */
const AVISO_GOOGLE_VENCIDO =
  "Pasó mucho tiempo desde que entraste con Google y ese vínculo ya no vale. " +
  "No se pierde nada de lo que contestaste: apretá 'Pedir acceso' otra vez y " +
  "mandamos la solicitud igual. Cuando la aprobemos vas a entrar definiendo " +
  "una contraseña, en vez de con el botón de Google.";

export function AccessRequestForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const montadoEn = useMemo(() => Date.now(), []);
  const websiteRef = useRef<HTMLInputElement>(null); // honeypot
  const crear = useCreateAccessRequest();

  const [draft, setDraft] = useState<AccessRequestDraft>(EMPTY_ACCESS_REQUEST_DRAFT);
  const [enviado, setEnviado] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  // Qué campos ya tocó el usuario, y si hubo un intento de envío. Sin esto el
  // formulario abriría con los tres campos de texto en rojo antes de que el
  // visitante escriba una letra.
  const [tocados, setTocados] = useState<Record<string, true>>({});
  const [intentado, setIntentado] = useState(false);

  // `?plan=` precarga la intención de plan pero la deja EDITABLE: el visitante
  // llegó desde el card de /precios, no firmó nada.
  const planDeUrl = searchParams.get("plan");
  useEffect(() => {
    if (isRequestedPlan(planDeUrl)) {
      setDraft((d) => (d.requested_plan ? d : { ...d, requested_plan: planDeUrl }));
    }
  }, [planDeUrl]);

  // `?prefill=` viene de "Continuar con Google". El token solo se devuelve en el
  // POST si el prefill respondió: mandar uno que sabemos muerto no liga nada, y
  // mandarlo con otro email sería un 403 después de un formulario largo.
  const prefillDeUrl = searchParams.get("prefill");
  const [tokenGoogle, setTokenGoogle] = useState<string | null>(null);
  useEffect(() => {
    if (!prefillDeUrl) return;
    let vigente = true;
    void (async () => {
      try {
        const identidad = await fetchGooglePrefill(prefillDeUrl);
        if (!vigente) return;
        // Merge parcial: toca SOLO email y nombre. Cualquier otra respuesta que
        // el visitante ya haya dado —incluida la de `?plan=`— sobrevive.
        setDraft((d) => ({
          ...d,
          email: identidad.email,
          full_name: d.full_name || identidad.full_name || "",
        }));
        setTokenGoogle(prefillDeUrl);
      } catch {
        // Token vencido, ya canjeado o inexistente: formulario a mano, sin
        // prometer un linkeo que no vamos a poder hacer.
        if (vigente) setTokenGoogle(null);
      }
    })();
    return () => {
      vigente = false;
    };
  }, [prefillDeUrl]);
  const emailVerificadoPorGoogle = tokenGoogle !== null;
  const hintEmail = emailVerificadoPorGoogle ? HINT_EMAIL_GOOGLE : undefined;
  // Se prende cuando el prefill se cayó entre el montaje y el envío.
  const [googleVencido, setGoogleVencido] = useState(false);

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

  function marcarTocado(campo: keyof AccessRequestDraft) {
    setTocados((t) => (t[campo] ? t : { ...t, [campo]: true }));
  }

  /**
   * Para controles de un solo gesto (radios, tarjetas, casilla): elegir ES
   * tocar, así que el error del campo se apaga en el mismo click.
   */
  function elegir<K extends keyof AccessRequestDraft>(
    key: K,
    value: AccessRequestDraft[K],
  ) {
    update(key, value);
    marcarTocado(key);
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
    marcarTocado("requested_vertical");
  }

  // Memoizado: sin esto el schema entero se re-valida en cada render, incluso
  // en los que no cambian el borrador.
  const parse = useMemo(() => parseAccessRequestDraft(draft), [draft]);
  const todosLosErrores = useMemo(() => fieldErrors(parse), [parse]);
  // Solo se muestran los errores de campos ya tocados (o todos, tras intentar
  // enviar). Es la mitad "no grites antes de tiempo" de la regla.
  const errores: Record<string, string> = {};
  for (const [campo, mensaje] of Object.entries(todosLosErrores)) {
    if (intentado || tocados[campo]) errores[campo] = mensaje;
  }

  /** Campos requeridos sin contestar, en el orden en el que aparecen. */
  const faltantes = ACCESS_REQUEST_FIELD_LABELS.map(([campo]) => campo).filter(
    (campo) => campo in todosLosErrores,
  );
  // Solo tras un intento de envío: con el primer blur se abría un panel "Te
  // faltan 12 respuestas" cuando el usuario iba por el campo 1 — la versión
  // atenuada de gritar antes de tiempo. Y al ser `role="status"` (live region)
  // se re-anunciaba entero con cada respuesta.
  const enviando = crear.isPending || enviado;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (enviando) return; // anti doble-submit
    if (!parse.success) {
      // Alcanzable por envío implícito (Enter) aunque el botón esté gris.
      // Destapa TODOS los errores y manda el foco al primero que falta.
      setIntentado(true);
      if (faltantes[0]) enfocarCampo(faltantes[0]);
      return;
    }

    const ctaSource = ctaSourceFromUrl("solicitar_acceso");
    setErrorMsg("");

    /*
     * El prefill de Google se lee al montar y el formulario es largo: para
     * cuando el visitante aprieta enviar, el token puede haber vencido.
     * Mandarlo igual no rompe nada —el backend guarda la solicitud sin
     * `google_subject`— pero deja al visitante creyendo que entró por Google
     * cuando en realidad va a tener que definir una contraseña, y nadie se lo
     * dice nunca. Eso es afirmar un estado que el sistema no sostiene.
     *
     * Revalidar acá es barato: `GET /access-requests/prefill/{token}` es una
     * lectura y NO consume el token (el GETDEL lo hace el POST). Si falla,
     * degradamos la UI, contamos qué implica y NO enviamos todavía: el aviso
     * tiene que llegar a leerse, y navegando a `/solicitud-enviada` en el
     * mismo gesto no se leería. El siguiente click envía sin token.
     */
    if (tokenGoogle) {
      try {
        await fetchGooglePrefill(tokenGoogle);
      } catch {
        setTokenGoogle(null);
        setGoogleVencido(true);
        trackLandingEvent("access_request_google_prefill_expired", {
          cta_source: ctaSource,
        });
        return;
      }
    }

    try {
      await crear.mutateAsync(
        buildAccessRequestPayload(parse.data, {
          ctaSource,
          website: websiteRef.current?.value ?? "",
          elapsedMs: Date.now() - montadoEn,
          googlePrefillToken: tokenGoogle ?? undefined,
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
        {/*
          Honeypot: oculto para humanos, tentador para bots.

          El `name` NO es `website`: ese es exactamente el campo que el autofill
          de Chrome y los gestores de contraseñas completan solos, y un
          honeypot lleno descarta la solicitud en silencio (`looks_like_bot()`
          no persiste nada y devuelve el mismo 201 genérico). `empresa_url` no
          está en ningún diccionario de autofill, y `new-password` desalienta
          al resto. Lo que viaja en el POST se sigue llamando `website`: el
          valor se lee por ref, el `name` del DOM no lo determina.
        */}
        <input
          ref={websiteRef}
          type="text"
          name="empresa_url"
          tabIndex={-1}
          autoComplete="new-password"
          aria-hidden
          className="hidden"
        />

        {/* ── Contacto ─────────────────────────────────────────────────────── */}
        <section className="space-y-5">
          <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
            Contacto
          </h2>

          <Field
            campo="full_name"
            label="Nombre y apellido"
            required
            error={errores.full_name}
          >
            <input
              {...fieldAria("full_name", { error: errores.full_name })}
              className={inputClass}
              maxLength={200}
              value={draft.full_name}
              onChange={(e) => update("full_name", e.target.value)}
              onBlur={() => marcarTocado("full_name")}
            />
          </Field>

          <Field
            campo="email"
            label="Email"
            required
            hint={hintEmail}
            error={errores.email}
          >
            <input
              {...fieldAria("email", { error: errores.email, hint: hintEmail })}
              type="email"
              className={inputClass}
              maxLength={255}
              value={draft.email}
              /*
               * Con prefill de Google el email NO se edita: es lo que liga la
               * solicitud a esa identidad, y cambiarlo acá haría que el backend
               * rechace el canje del token (403 `google_prefill_email_mismatch`).
               */
              readOnly={emailVerificadoPorGoogle}
              onChange={(e) => update("email", e.target.value)}
              onBlur={() => marcarTocado("email")}
            />
          </Field>

          <Field
            campo="phone"
            label="Teléfono / WhatsApp (opcional)"
            hint={HINT_TELEFONO}
            error={errores.phone}
          >
            <input
              {...fieldAria("phone", { error: errores.phone, hint: HINT_TELEFONO })}
              className={inputClass}
              maxLength={50}
              placeholder="+54 9 11 1234 5678"
              value={draft.phone}
              onChange={(e) => update("phone", e.target.value)}
              onBlur={() => marcarTocado("phone")}
            />
          </Field>

          <Field
            campo="business_name"
            label="Nombre del negocio"
            required
            error={errores.business_name}
          >
            <input
              {...fieldAria("business_name", { error: errores.business_name })}
              className={inputClass}
              maxLength={200}
              value={draft.business_name}
              onChange={(e) => update("business_name", e.target.value)}
              onBlur={() => marcarTocado("business_name")}
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
              {REQUESTED_VERTICAL_OPTIONS.map((opcion, indice) => {
                const seleccionado = draft.requested_vertical === opcion.code;
                return (
                  <button
                    key={opcion.code}
                    id={indice === 0 ? fieldAnchorId("requested_vertical") : undefined}
                    type="button"
                    aria-pressed={seleccionado}
                    onClick={() => seleccionarRubro(opcion.code)}
                    className={[
                      "flex flex-col items-start gap-3 rounded-xl border-2 p-4 text-left transition-all duration-150",
                      seleccionado
                        ? "border-vektor-blue bg-vektor-surface"
                        : errores.requested_vertical
                          ? "border-vektor-red/60 hover:border-vektor-blue/50"
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
              campo="vertical_other_text"
              label="Contanos de qué es tu negocio"
              required
              error={errores.vertical_other_text}
            >
              <textarea
                {...fieldAria("vertical_other_text", {
                  error: errores.vertical_other_text,
                })}
                className={`${inputClass} min-h-[90px] resize-y`}
                maxLength={2000}
                value={draft.vertical_other_text}
                onChange={(e) => update("vertical_other_text", e.target.value)}
                onBlur={() => marcarTocado("vertical_other_text")}
              />
            </Field>
          )}
        </section>

        <BusinessScreeningFields draft={draft} update={elegir} errores={errores} />

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
            onChange={(v) => elegir("requested_plan", v)}
            error={errores.requested_plan}
            columns={1}
          />
        </section>

        {/* ── Consentimiento ───────────────────────────────────────────────── */}
        <div className="space-y-5">
          <label className="flex items-start gap-3 text-sm text-vektor-body">
            <input
              id={fieldAnchorId("consent")}
              type="checkbox"
              className="mt-1 accent-vektor-blue"
              checked={draft.consent}
              onChange={(e) => elegir("consent", e.target.checked)}
            />
            <span>
              Acepto la{" "}
              <Link href="/privacidad" className="text-vektor-blue hover:underline">
                política de privacidad
              </Link>{" "}
              y el tratamiento de mis datos para que revisen mi solicitud.
            </span>
          </label>

          {errores.consent && (
            <p className="text-xs text-vektor-red" role="alert">
              {errores.consent}
            </p>
          )}

          {/*
            Resumen de faltantes: es lo que convierte un botón gris en una
            instrucción. Cada faltante lleva el foco a su campo, así el usuario
            no tiene que barrer cinco secciones a ojo.
          */}
          {intentado && faltantes.length > 0 && (
            <div
              role="status"
              className="rounded-xl border border-vektor-border bg-vektor-surface/60 p-4 text-sm text-vektor-body"
            >
              <p className="font-semibold text-vektor-white">
                {faltantes.length === 1
                  ? "Te falta responder una cosa:"
                  : `Te faltan ${faltantes.length} respuestas:`}
              </p>
              <ul className="mt-2 flex flex-wrap gap-x-2 gap-y-1">
                {faltantes.map((campo) => (
                  <li key={campo}>
                    <button
                      type="button"
                      onClick={() => {
                        setIntentado(true);
                        enfocarCampo(campo);
                      }}
                      className="text-vektor-blue underline-offset-2 hover:underline"
                    >
                      {accessRequestFieldLabel(campo)}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/*
            No es un error del visitante ni un fallo del envío: es un cambio de
            estado que le cambia cómo va a entrar. Va en tono de aviso, no en
            rojo, y no bloquea nada.
          */}
          {googleVencido && (
            <p
              role="alert"
              className="rounded-xl border border-vektor-border bg-vektor-surface/60 p-4 text-sm leading-relaxed text-vektor-body"
            >
              {AVISO_GOOGLE_VENCIDO}
            </p>
          )}

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
