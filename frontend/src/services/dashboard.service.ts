import { api } from "@/lib/api";
import { clasificarLatestScore } from "@/services/health_score.service";
import type {
  LatestScoreResponse,
  CurrentInsightResponse,
  BusinessBreakdownResponse,
  CashForecastResponse,
  HealthScoreV2Response,
  MarginsResponse,
  ConfirmarAperturaResponse,
} from "@/types/api";

/**
 * M·3-2: los márgenes que pinta la pantalla, todos de FactsService. Sin rango,
 * el mes en curso; el neto es siempre del último mes cerrado.
 */
export async function fetchMargins(range?: { from: string; to: string }): Promise<MarginsResponse> {
  const { data } = await api.get<MarginsResponse>("/insights/margins", {
    params: range ? { from_date: range.from, to_date: range.to } : undefined,
  });
  return data;
}

/**
 * M·2d: el usuario dice desde cuándo tiene el stock inicial. Por archivo
 * (todas las cargas de ese archivo) o por producto (las cargadas a mano). Pide
 * PIN (lo resuelve el interceptor de `api`).
 */
/**
 * M·2d-1b: `aceptarFechasDeFilas` confirma con su fecha el stock que la trae en
 * su fila; `fecha` es la del resto (sin ella y con alguno sin fecha → 422).
 */
export async function confirmarAperturaDeArchivo(
  uploadId: string,
  fecha: string | null,
  aceptarFechasDeFilas = false,
): Promise<ConfirmarAperturaResponse> {
  const { data } = await api.post<ConfirmarAperturaResponse>(
    `/ingestion/files/${uploadId}/opening-confirmation`,
    { fecha, aceptar_fechas_de_filas: aceptarFechasDeFilas },
  );
  return data;
}

export async function confirmarAperturaDeProducto(
  productId: string,
  fecha: string,
): Promise<ConfirmarAperturaResponse> {
  const { data } = await api.post<ConfirmarAperturaResponse>(
    `/products/${productId}/opening-confirmation`,
    { fecha },
  );
  return data;
}

export async function fetchLatestScore(): Promise<LatestScoreResponse> {
  const { data } = await api.get<LatestScoreResponse>("/health-scores/latest");
  return data;
}

/**
 * Type predicate sobre la unión de tres estados de `/health-scores/latest`.
 *
 * Existe para que el dashboard no necesite `as HealthScoreV2Response`: con el
 * cast puesto, el compilador no obligaba a cubrir `CALCULATING` ni `NO_DATA`
 * —la seguridad dependía por completo de acordarse de chequear en runtime— y
 * agregar un cuarto estado en el backend no habría dado ni un error de
 * compilación. La clasificación NO se reimplementa acá: delega en
 * `clasificarLatestScore`, que es la única definición.
 */
export function esScoreReal(
  data: LatestScoreResponse | undefined,
): data is HealthScoreV2Response {
  return clasificarLatestScore(data) === "score";
}

export async function fetchCurrentInsight(): Promise<CurrentInsightResponse> {
  const { data } = await api.get<CurrentInsightResponse>("/insights/current");
  return data;
}

export async function acknowledgeAction(id: string): Promise<void> {
  await api.patch(`/insights/actions/${id}/acknowledge`);
}

export async function fetchBusinessBreakdown(days = 30): Promise<BusinessBreakdownResponse> {
  const { data } = await api.get<BusinessBreakdownResponse>(`/insights/breakdown?days=${days}`);
  return data;
}

export async function fetchCashForecast(refresh = false): Promise<CashForecastResponse> {
  const { data } = await api.get<CashForecastResponse>(
    `/forecast/cash${refresh ? "?refresh=true" : ""}`,
  );
  return data;
}

export async function fetchHealthScoreHistory(): Promise<HealthScoreV2Response[]> {
  const { data } = await api.get<HealthScoreV2Response[]>("/health-scores/history/v2");
  return data;
}
