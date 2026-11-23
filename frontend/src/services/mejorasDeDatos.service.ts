import { api } from "@/lib/api";
import type {
  MejoraAplicarResponse,
  MejoraRevertirResponse,
  MejoraVistaPrevia,
  MejorasDeDatosPanel,
} from "@/types/api";

/** Pendientes y lo aplicado en los últimos 14 días. SELECT-only en el servidor. */
export async function fetchMejorasDeDatos(): Promise<MejorasDeDatosPanel> {
  const { data } = await api.get<MejorasDeDatosPanel>("/data-improvements");
  return data;
}

/**
 * Lo que se aplicaría hoy, con los números que verá el dueño. Algunas mejoras releen
 * el archivo original (S3): puede tardar. No escribe nada.
 */
export async function fetchVistaPrevia(mejoraId: string): Promise<MejoraVistaPrevia> {
  const { data } = await api.get<MejoraVistaPrevia>(`/data-improvements/${mejoraId}/preview`);
  return data;
}

/** Dispara las mejoras que sólo completan vacíos. Sin PIN; encola una tarea por tenant. */
export async function dispararMejorasAutomaticas(): Promise<{ queued: boolean }> {
  const { data } = await api.post<{ queued: boolean }>("/data-improvements/auto");
  return data;
}

/** Aplica una mejora (la que cambia valores pide PIN: lo resuelve el interceptor de `api`). */
export async function aplicarMejora(
  mejoraId: string,
  ids: string[] | null = null,
): Promise<MejoraAplicarResponse> {
  const { data } = await api.post<MejoraAplicarResponse>(
    `/data-improvements/${mejoraId}/apply`,
    { ids },
  );
  return data;
}

export async function revertirMejora(runId: string): Promise<MejoraRevertirResponse> {
  const { data } = await api.post<MejoraRevertirResponse>(
    `/data-improvements/runs/${runId}/revert`,
  );
  return data;
}
