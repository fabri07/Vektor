import axios from "axios";

/**
 * B14 del POS: con la suscripción bloqueada el backend PAUSA el análisis
 * (score, insights, proyección, balance, momentum) con 402
 * `SUBSCRIPTION_INSIGHTS_LOCKED`. Los datos, sus correcciones y la caja
 * siguen andando.
 *
 * El aviso se deriva del error de CADA query, no de un estado global: así,
 * al cambiar de cuenta (`useSessionSync` vacía la caché) o al volver a pedir
 * los datos después de regularizar, desaparece solo, sin nada que limpiar.
 */
export const CODIGO_ANALISIS_BLOQUEADO = "SUBSCRIPTION_INSIGHTS_LOCKED";

export function esAnalisisBloqueado(error: unknown): boolean {
  if (!axios.isAxiosError(error) || error.response?.status !== 402) return false;
  const data = error.response.data as
    | { code?: string; detail?: { code?: string } | string }
    | undefined;
  if (data?.code === CODIGO_ANALISIS_BLOQUEADO) return true;
  const detail = data?.detail;
  return typeof detail === "object" && detail?.code === CODIGO_ANALISIS_BLOQUEADO;
}

/**
 * `retry` de TanStack para las lecturas analíticas: un 402 no se arregla
 * reintentando —hay que regularizar la suscripción—, así que no se reintenta;
 * cualquier otro error, una vez, como el resto de la app.
 */
export function reintentarSalvoBloqueo(fallas: number, error: unknown): boolean {
  if (esAnalisisBloqueado(error)) return false;
  return fallas < 1;
}
