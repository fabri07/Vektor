/**
 * Persistencia de preferencias de columnas de `SmartTable` — Cambio 2 del plan
 * de conservación y acceso a datos de negocio (docs/plans/
 * conservacion-y-acceso-datos-negocio.md): "permitir ocultar/mostrar y
 * restablecer columnas... persistir preferencias locales bajo una clave
 * versionada por tenant + usuario + sección".
 *
 * `localStorage`, no `sessionStorage`: a diferencia del borrador de acceso
 * (datos sensibles de un formulario ajeno), esto es una preferencia de UI sin
 * datos personales — tiene sentido que sobreviva a cerrar la pestaña. Son
 * preferencias de ESTE navegador; cambiar de dispositivo no las hereda, y esa
 * limitación es a propósito (no hay backend para esto todavía).
 */

export interface ColumnPreferences {
  /** Claves de columnas visibles, tal como las dejó el usuario. */
  visible: string[];
  /** Claves de columnas que el usuario ya vio alguna vez — lo que NO está acá
   * es "nuevo" (recién declarado por una importación, por ejemplo). */
  known: string[];
  /** Opcional para leer preferencias v1 previas a la incorporación del orden. */
  order?: string[];
}

function claveCompleta(storageKey: string): string {
  return `vektor:columns:v1:${storageKey}`;
}

export function leerPreferenciasDeColumnas(storageKey: string): ColumnPreferences | null {
  if (typeof window === "undefined") return null;
  try {
    const crudo = window.localStorage.getItem(claveCompleta(storageKey));
    if (!crudo) return null;
    const parseado: unknown = JSON.parse(crudo);
    if (typeof parseado !== "object" || parseado === null) return null;
    const { visible, known, order } = parseado as Record<string, unknown>;
    if (!Array.isArray(visible) || !Array.isArray(known)) return null;
    if (!visible.every((v) => typeof v === "string") || !known.every((k) => typeof k === "string")) {
      return null;
    }
    return {
      visible, known,
      ...(Array.isArray(order) && order.every((key) => typeof key === "string") ? { order } : {}),
    };
  } catch {
    return null;
  }
}

export function guardarPreferenciasDeColumnas(
  storageKey: string,
  prefs: ColumnPreferences,
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(claveCompleta(storageKey), JSON.stringify(prefs));
  } catch {
    // Cuota llena, modo privado, storage bloqueado por política: perder la
    // preferencia de columnas no vale romper la tabla.
  }
}

export function borrarPreferenciasDeColumnas(storageKey: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(claveCompleta(storageKey));
  } catch {
    // Idem arriba.
  }
}
