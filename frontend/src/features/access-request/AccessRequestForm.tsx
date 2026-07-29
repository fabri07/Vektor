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
 * es el POST —, y por eso el submit lo revalida antes de mandar: el token puede
 * haber vencido mientras se completaban las trece respuestas, y seguir afirmando que
 * Google verificó el email sería afirmar un estado que el sistema ya no tiene.
 *
 * Anti-spam en capas, igual que `/contacto`: honeypot invisible (`empresa_url`,
 * ver más abajo por qué NO se llama `website`) + `elapsed_ms` medido desde el
 * montaje. El rate limit por IP lo pone el backend.
 *
 * **Superficie de errores.** El formulario arranca en silencio: un campo solo
 * muestra su error después de que el usuario lo tocó (`onBlur` en los textos,
 * la selección en los grupos) o después de un intento de envío. Al revés,
 * ningún campo requerido se queda mudo: los 9 grupos de opción muestran el
 * suyo, y arriba del botón hay un resumen de lo que falta con foco directo a
 * cada campo. Las dos mitades son la misma regla — decir lo que falta, cuando
 * corresponde, y nada más.
 *
 * Y para que esa segunda mitad exista, **el botón de envío nunca se deshabilita
 * por formulario incompleto**: intentar enviar es el gesto con el que el
 * visitante pregunta qué le falta. Solo se bloquea mientras hay un envío en
 * curso.
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
  REQUIRED_FIELD_COUNT,
  type AccessRequestDraft,
} from "@/validation/accessRequest";
import { borrarBorrador, guardarBorrador, leerBorrador } from "./draftStorage";
import {
  BusinessScreeningFields,
  Field,
  RadioGroup,
  describeAria,
  fieldAnchorId,
  fieldAria,
  inputClass,
} from "./BusinessScreeningFields";

/** Respeta `prefers-reduced-motion`: sin animación para quien la desactivó. */
function comportamientoDeScroll(): ScrollBehavior {
  if (typeof window === "undefined" || !window.matchMedia) return "smooth";
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

/** Lleva el foco (y la vista) al elemento, sin doble scroll. */
function enfocarYMostrar(elemento: HTMLElement) {
  elemento.focus({ preventScroll: true });
  elemento.scrollIntoView({ block: "center", behavior: comportamientoDeScroll() });
}

/** Lleva el foco (y la vista) al primer control del campo indicado. */
function enfocarCampo(campo: string) {
  const control = document.getElementById(fieldAnchorId(campo));
  if (!control) return;
  enfocarYMostrar(control);
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

  /*
   * El borrador arranca de lo que haya en `sessionStorage`.
   *
   * Va como inicializador perezoso de `useState` —y no en un `useEffect`—
   * porque el efecto del prefill de Google hace un merge parcial sobre el
   * borrador: si la hidratación corriera después, pisaría el email que Google
   * ya verificó. Acá el orden queda garantizado por construcción.
   */
  const [draft, setDraft] = useState<AccessRequestDraft>(() => ({
    ...EMPTY_ACCESS_REQUEST_DRAFT,
    ...(leerBorrador() ?? {}),
  }));
  const [enviado, setEnviado] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  // Qué campos ya tocó el usuario, y si hubo un intento de envío. Sin esto el
  // formulario abriría con los tres campos de texto en rojo antes de que el
  // visitante escriba una letra.
  const [tocados, setTocados] = useState<Record<string, true>>({});
  const [intentado, setIntentado] = useState(false);
  /*
   * Cuenta de envíos rechazados por incompletos. Es un contador y no un
   * booleano porque el foco tiene que volver al resumen en CADA intento, no
   * solo en el primero: quien aprieta enviar dos veces sin completar nada
   * necesita que se lo digan las dos veces.
   */
  const [intentosFallidos, setIntentosFallidos] = useState(0);
  const resumenTituloRef = useRef<HTMLParagraphElement>(null);

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

  // Cada cambio del borrador se persiste. Es barato (un JSON chico en
  // sessionStorage) y evita tener que elegir "momentos buenos" para guardar,
  // que es como se pierden justo las últimas respuestas.
  useEffect(() => {
    guardarBorrador(draft);
  }, [draft]);

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

  /*
   * Navegación con flechas del grupo de rubros, que un `<input type="radio">`
   * traería gratis. Mover el foco TAMBIÉN elige: es lo que hace un radio
   * nativo, y es lo que un lector de pantalla anuncia al recorrer el grupo.
   */
  const rubroRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const indiceRubroSeleccionado = REQUESTED_VERTICAL_OPTIONS.findIndex(
    (o) => o.code === draft.requested_vertical,
  );
  // Sin elección todavía, el tabstop del grupo es la primera opción.
  const indiceRubroTabulable = indiceRubroSeleccionado === -1 ? 0 : indiceRubroSeleccionado;

  function moverEntreRubros(evento: React.KeyboardEvent, desde: number) {
    const total = REQUESTED_VERTICAL_OPTIONS.length;
    const paso =
      evento.key === "ArrowRight" || evento.key === "ArrowDown"
        ? 1
        : evento.key === "ArrowLeft" || evento.key === "ArrowUp"
          ? -1
          : 0;
    if (paso === 0) return;
    evento.preventDefault(); // sin esto, las flechas scrollean la página
    const destino = (desde + paso + total) % total;
    seleccionarRubro(REQUESTED_VERTICAL_OPTIONS[destino]!.code);
    rubroRefs.current[destino]?.focus();
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
  // Acotado por abajo: con "otros" elegido y sin describirlo aparece un campo
  // requerido de más, y sin el clamp el contador diría "-1 de 13".
  const respondidas = Math.max(0, REQUIRED_FIELD_COUNT - faltantes.length);
  // Solo tras un intento de envío: con el primer blur se abría un panel "Te
  // faltan 12 respuestas" cuando el usuario iba por el campo 1 — la versión
  // atenuada de gritar antes de tiempo. Y al ser `role="status"` (live region)
  // se re-anunciaba entero con cada respuesta.
  const enviando = crear.isPending || enviado;

  /*
   * Tras un envío rechazado por incompleto, el foco va al TÍTULO del resumen,
   * no al primer campo faltante: ver la lista entera antes de saltar es más
   * informativo que aterrizar a ciegas en el campo 4 sin saber cuántos quedan.
   *
   * Va en un efecto y no en el handler porque el resumen se monta recién
   * después de que `intentado` se aplique al render.
   *
   * El contenedor lleva `role="alert"` y el foco cae en un hijo, no en el
   * mismo nodo, para no pedir dos anuncios del mismo texto. Si la prueba con
   * VoiceOver/NVDA igual muestra lectura duplicada, lo que sobra es el
   * `role="alert"`: el movimiento de foco ya anuncia por sí solo.
   */
  useEffect(() => {
    if (intentosFallidos === 0) return;
    const titulo = resumenTituloRef.current;
    if (titulo) enfocarYMostrar(titulo);
  }, [intentosFallidos]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (enviando) return; // anti doble-submit
    if (!parse.success) {
      // El botón NO está deshabilitado por incompleto justamente para que se
      // pueda llegar hasta acá: destapa todos los errores y el efecto de abajo
      // manda el foco al resumen.
      setIntentado(true);
      setIntentosFallidos((n) => n + 1);
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
      // La solicitud ya está en el backend: el borrador cumplió su función y
      // no tiene por qué quedar esperando en el navegador.
      borrarBorrador();
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

          {/*
            Las tarjetas son botones, no `<input type="radio">`, porque llevan
            icono y descripción. Eso obliga a reponer a mano todo lo que un
            radio nativo trae gratis: rol de grupo, `aria-checked`, un solo
            tabstop para todo el grupo y navegación con flechas.

            Antes eran `aria-pressed` (patrón de interruptor): un lector las
            anunciaba como seis botones independientes que se prenden y apagan,
            no como "opción 2 de 6" de una elección única. Y cada tarjeta era
            su propio tabstop.
          */}
          <fieldset
            role="radiogroup"
            aria-labelledby="rubro-legend"
            {...describeAria("requested_vertical", {
              error: errores.requested_vertical,
            })}
          >
            <legend
              id="rubro-legend"
              className="mb-3 block text-sm font-medium text-vektor-body"
            >
              ¿De qué es tu negocio? <span className="text-vektor-red">*</span>
            </legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {REQUESTED_VERTICAL_OPTIONS.map((opcion, indice) => {
                const seleccionado = draft.requested_vertical === opcion.code;
                return (
                  <button
                    key={opcion.code}
                    id={indice === 0 ? fieldAnchorId("requested_vertical") : undefined}
                    ref={(nodo) => {
                      rubroRefs.current[indice] = nodo;
                    }}
                    type="button"
                    role="radio"
                    aria-checked={seleccionado}
                    // Roving tabindex: el grupo entero es UN tabstop. Entra en
                    // la opción elegida, o en la primera si no hay ninguna.
                    tabIndex={indice === indiceRubroTabulable ? 0 : -1}
                    onKeyDown={(evento) => moverEntreRubros(evento, indice)}
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
            {/*
              El error del rubro era SOLO un borde rojo. Feedback exclusivo por
              color: invisible para un lector de pantalla y falla WCAG 1.4.1.
            */}
            {errores.requested_vertical && (
              <p
                id={`${fieldAnchorId("requested_vertical")}-error`}
                className="mt-1.5 text-xs text-vektor-red"
              >
                {errores.requested_vertical}
              </p>
            )}
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
              {...fieldAria("consent", { error: errores.consent })}
              type="checkbox"
              className="mt-0.5 h-5 w-5 shrink-0 accent-vektor-blue"
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
            <p id={`${fieldAnchorId("consent")}-error`} className="text-xs text-vektor-red">
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
              role="alert"
              className="rounded-xl border border-vektor-border bg-vektor-surface/60 p-4 text-sm text-vektor-body"
            >
              {/*
                `tabIndex={-1}` lo hace enfocable por código sin meterlo en el
                orden de tabulación. Conserva anillo de foco: quien navega con
                teclado tiene que VER adónde saltó, no solo escucharlo.
              */}
              <p
                ref={resumenTituloRef}
                tabIndex={-1}
                className="rounded font-semibold text-vektor-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-vektor-blue"
              >
                {faltantes.length === 1
                  ? "Te falta responder una cosa:"
                  : `Te faltan ${faltantes.length} respuestas:`}
              </p>
              <ul className="mt-2 flex flex-wrap gap-x-2 gap-y-1">
                {faltantes.map((campo) => (
                  <li key={campo}>
                    <button
                      type="button"
                      onClick={() => enfocarCampo(campo)}
                      className="inline-block py-1.5 text-vektor-blue underline-offset-2 hover:underline"
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

          {/*
            Cuántas van. Deliberadamente NO es sticky: un contador fijo en
            móvil tapa el último campo o el propio resumen de errores, compite
            con el CTA y obliga a lidiar con safe areas. Acá abajo, pegado al
            botón, se lee justo cuando importa — antes de apretar.

            El total sale del schema (`REQUIRED_FIELD_COUNT`), no de un número
            escrito a mano.
          */}
          <p className="text-center text-xs text-vektor-muted">
            {respondidas} de {REQUIRED_FIELD_COUNT} respuestas
          </p>

          {/*
            El botón NO se deshabilita por formulario incompleto.

            Deshabilitarlo era el bug: un submit `disabled` esconde el motivo
            del fallo, y encima el HTML Standard no dispara envío implícito
            cuando el botón por defecto existe y está deshabilitado — así que
            ni con Enter se llegaba al handler. El resumen de faltantes, el
            foco y los errores de los nueve grupos eran código inalcanzable.

            Solo se deshabilita mientras hay un envío en curso, que es la única
            razón real para no aceptar otro click.
          */}
          <button
            type="submit"
            disabled={enviando}
            className="inline-flex w-full items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue-strong to-vektor-teal-deep px-6 py-3.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-wait"
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
