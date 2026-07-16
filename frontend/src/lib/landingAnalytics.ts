/**
 * Analytics mínimo para la landing pública — sin PII, sin dependencias.
 *
 * Empuja eventos a `window.dataLayer` (compatible con GTM/GA4 si el sitio los
 * carga); si no existe, es un no-op silencioso. NUNCA se envían datos personales
 * del visitante (nombre, email, teléfono): solo el nombre del evento y metadata
 * no identificable como `cta_source`.
 */

type EventParams = Record<string, string | number | boolean | undefined>;

interface DataLayerWindow extends Window {
  dataLayer?: Array<Record<string, unknown>>;
}

/** Registra un evento de la landing. No-op fuera del browser o sin dataLayer. */
export function trackLandingEvent(event: string, params: EventParams = {}): void {
  if (typeof window === "undefined") return;
  const w = window as DataLayerWindow;
  if (!Array.isArray(w.dataLayer)) return; // no-op si no hay analytics cargado
  // Filtrar undefined para no ensuciar el dataLayer.
  const clean: Record<string, unknown> = { event };
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined) clean[k] = v;
  }
  w.dataLayer.push(clean);
}

/**
 * Deriva el origen del CTA desde `?src=` de la URL (fallback al valor dado).
 * Permite medir desde qué botón llegó el visitante al formulario.
 */
export function ctaSourceFromUrl(fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const src = new URLSearchParams(window.location.search).get("src");
  return src && src.length <= 60 ? src : fallback;
}
