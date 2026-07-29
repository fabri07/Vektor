/**
 * Persistencia del borrador de la solicitud de acceso.
 *
 * El formulario tiene trece respuestas requeridas y lleva unos tres minutos.
 * Sin esto, un F5, un back accidental o un link tocado sin querer borraba
 * todo — y como el visitante no tiene cuenta todavía, no hay ningún otro lugar
 * del que recuperarlo.
 *
 * **`sessionStorage`, no `localStorage`.** Acá adentro hay nombre, email,
 * teléfono y datos del negocio, y el formulario a veces se completa en una
 * máquina compartida. Sobrevive al refresh y a la navegación dentro de la
 * pestaña; muere cuando la pestaña se cierra. Es el alcance que resuelve el
 * problema real sin dejar datos personales esperando al próximo que se siente.
 */

import {
  EMPTY_ACCESS_REQUEST_DRAFT,
  type AccessRequestDraft,
} from "@/validation/accessRequest";

/**
 * Clave versionada. Si cambia la forma del borrador, se sube el número y lo
 * guardado con el formato viejo se descarta en silencio en vez de hidratar
 * campos que ya no existen.
 */
const CLAVE = "vektor:access-request-draft:v1";

/**
 * Lo que NO se persiste.
 *
 * `consent` queda afuera a propósito: aceptar la política de privacidad tiene
 * que ser un acto de esta sesión, no algo que aparezca ya tildado porque el
 * navegador se acuerda. Restaurarlo sería afirmar un consentimiento que el
 * visitante no dio hoy.
 *
 * El token de prefill de Google tampoco se guarda, pero por otro motivo: no
 * vive en el borrador (es estado aparte del formulario) y es de un solo uso.
 */
const NO_PERSISTIDOS = ["consent"] as const;

export function guardarBorrador(draft: AccessRequestDraft): void {
  if (typeof window === "undefined") return;
  try {
    const { consent: _consent, ...resto } = draft;
    window.sessionStorage.setItem(CLAVE, JSON.stringify(resto));
  } catch {
    // Cuota llena, modo privado de Safari, storage bloqueado por política:
    // perder el borrador es malo, romper el formulario es peor.
  }
}

/**
 * Lee el borrador guardado. Devuelve `null` si no hay, si está corrupto, o si
 * lo guardado no se parece a un borrador.
 */
export function leerBorrador(): Partial<AccessRequestDraft> | null {
  if (typeof window === "undefined") return null;
  try {
    const crudo = window.sessionStorage.getItem(CLAVE);
    if (!crudo) return null;
    const parseado: unknown = JSON.parse(crudo);
    if (typeof parseado !== "object" || parseado === null) return null;

    // Se copian solo las claves que el borrador vacío declara, y solo si el
    // tipo coincide: lo guardado viene de una sesión anterior y no hay ninguna
    // garantía de que sea la misma versión del formulario.
    const limpio: Partial<AccessRequestDraft> = {};
    for (const clave of Object.keys(EMPTY_ACCESS_REQUEST_DRAFT)) {
      if ((NO_PERSISTIDOS as readonly string[]).includes(clave)) continue;
      const valor = (parseado as Record<string, unknown>)[clave];
      if (typeof valor === "string") {
        (limpio as Record<string, unknown>)[clave] = valor;
      }
    }
    return Object.keys(limpio).length > 0 ? limpio : null;
  } catch {
    return null;
  }
}

/** Borra el borrador. Se llama cuando la solicitud se envió de verdad. */
export function borrarBorrador(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(CLAVE);
  } catch {
    // Idem `guardarBorrador`: no vale romper el envío por esto.
  }
}
