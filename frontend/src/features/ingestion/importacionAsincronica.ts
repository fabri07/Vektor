/**
 * E6c-3 — el lado del cliente de la importación en segundo plano.
 *
 * La regla que gobierna este módulo
 * ---------------------------------
 * **Ante un timeout del POST, NUNCA se cae a `/confirm`.** Un timeout no dice que
 * la petición no llegó: dice que no sabemos si llegó. Si el backend la registró y
 * el cliente además dispara el confirm sincrónico, el archivo se importa dos
 * veces — que es exactamente lo que esta fase vino a impedir.
 *
 * Lo que sí se hace ante un timeout es **repetir la misma petición con la misma
 * clave**. El backend la reconoce y devuelve el intento que ya existe, o lo crea
 * si nunca llegó. Las dos ramas terminan en un solo intento.
 *
 * El único caso donde `/confirm` es seguro es un **404**: significa que la ruta no
 * está habilitada para este tenant, o sea que el servidor ni miró el cuerpo y no
 * hay nada registrado. Es una respuesta, no una incertidumbre. La diferencia entre
 * "el servidor dijo que no" y "no sabemos qué dijo el servidor" es la que decide
 * si duplicar datos es posible.
 *
 * La clave se persiste ANTES de mandar
 * ------------------------------------
 * Si la clave viviera sólo en memoria, un reload durante el timeout la perdería y
 * el reintento crearía un segundo intento — el problema que la clave existe para
 * evitar. Por eso se guarda primero y se manda después.
 */

const ALMACEN = "vektor.importaciones";

export type EstadoImportacion =
  | "PENDIENTE"
  | "EJECUTANDO"
  | "COMPLETADO"
  | "FALLADO"
  | "CANCELADO";

export interface ImportacionEnCurso {
  fileId: string;
  attemptId?: string;
  /** La clave de ESTA petición. Estable entre reintentos y entre recargas. */
  requestKey: string;
  creadaEn: number;
}

/** Los estados donde ya no hay nada que esperar. */
export const TERMINALES: ReadonlySet<EstadoImportacion> = new Set([
  "COMPLETADO",
  "FALLADO",
  "CANCELADO",
]);

export function esTerminal(estado: string): boolean {
  return TERMINALES.has(estado as EstadoImportacion);
}

function leerTodas(): Record<string, ImportacionEnCurso> {
  try {
    const crudo = window.localStorage.getItem(ALMACEN);
    return crudo ? (JSON.parse(crudo) as Record<string, ImportacionEnCurso>) : {};
  } catch {
    // localStorage puede fallar entero (modo privado, cuota, permisos). Perder la
    // recuperación al recargar es malo; romper la pantalla de importación es peor.
    return {};
  }
}

function escribirTodas(valor: Record<string, ImportacionEnCurso>): void {
  try {
    window.localStorage.setItem(ALMACEN, JSON.stringify(valor));
  } catch {
    /* ver leerTodas */
  }
}

/**
 * Genera la clave de petición y la guarda ANTES de que salga el POST.
 *
 * Si ya hay una importación en curso para este archivo, devuelve SU clave: es lo
 * que hace que un reintento tras un timeout —o después de un reload— sea la misma
 * petición y no una nueva.
 */
export function reservarClave(fileId: string): ImportacionEnCurso {
  const todas = leerTodas();
  const existente = todas[fileId];
  if (existente) return existente;

  const entrada: ImportacionEnCurso = {
    fileId,
    requestKey: nuevaClave(),
    creadaEn: Date.now(),
  };
  todas[fileId] = entrada;
  escribirTodas(todas);
  return entrada;
}

function nuevaClave(): string {
  // `randomUUID` no existe en contextos no seguros (http://…): sin el fallback,
  // la pantalla rompería justo en desarrollo y en redes internas.
  const uuid =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random()
          .toString(36)
          .slice(2)}`;
  return `imp-${uuid}`;
}

/** Guarda el id que devolvió el registro, para poder consultar tras un reload. */
export function recordarIntento(fileId: string, attemptId: string): void {
  const todas = leerTodas();
  const entrada = todas[fileId];
  if (!entrada) return;
  todas[fileId] = { ...entrada, attemptId };
  escribirTodas(todas);
}

/** La importación en curso de este archivo, si quedó alguna. */
export function importacionEnCurso(fileId: string): ImportacionEnCurso | null {
  return leerTodas()[fileId] ?? null;
}

/** Todas las que quedaron abiertas — para retomarlas al volver a la pantalla. */
export function importacionesEnCurso(): ImportacionEnCurso[] {
  return Object.values(leerTodas());
}

/** Se terminó (bien o mal): deja de seguirse. */
export function olvidarImportacion(fileId: string): void {
  const todas = leerTodas();
  delete todas[fileId];
  escribirTodas(todas);
}

/**
 * ¿Este error del POST permite caer a `/confirm`?
 *
 * **Sólo el 404**, que significa "la ruta no está habilitada": el servidor
 * respondió sin mirar el cuerpo, así que no registró nada y el confirm sincrónico
 * no puede duplicar.
 *
 * Todo lo demás —timeout, red caída, 5xx— es incertidumbre: la petición pudo
 * haberse registrado. Ahí se reintenta con la MISMA clave, nunca se cambia de
 * ruta.
 */
export function permiteCaerAlConfirmSincronico(error: unknown): boolean {
  const status = (error as { response?: { status?: number } })?.response?.status;
  return status === 404;
}

/**
 * Cuánto esperar antes de la próxima consulta.
 *
 * Arranca rápido —un archivo chico termina en segundos y hacer esperar cinco no
 * tiene sentido— y se separa hasta 5 s para no golpear al servidor durante una
 * importación de minutos. El tope importa: sin él, una pestaña olvidada consulta
 * cada segundo para siempre.
 */
export function proximaEspera(consultas: number): number {
  const base = 800;
  const tope = 5_000;
  return Math.min(tope, base * Math.pow(1.4, Math.max(0, consultas)));
}

/**
 * Qué mostrar según el estado. El texto lo lee alguien que está esperando: dice
 * qué está pasando, no en qué fase interna anda el ejecutor.
 */
export function textoDeEstado(estado: string, phase?: string | null): string {
  switch (estado) {
    case "PENDIENTE":
      return "En cola. La importación va a arrancar en unos segundos.";
    case "EJECUTANDO":
      return phase === "reclamado" || !phase
        ? "Importando…"
        : `Importando — ${phase}`;
    case "COMPLETADO":
      return "Importación terminada.";
    case "CANCELADO":
      return "La importación se canceló.";
    default:
      return "La importación no se pudo completar.";
  }
}
