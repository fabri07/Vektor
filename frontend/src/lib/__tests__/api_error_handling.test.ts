import type { AxiosError } from "axios";

/**
 * Una request CANCELADA a propósito (AbortController de React Query
 * descartando una consulta obsoleta, ej. ColumnMapperPanel recalculando
 * `riskRecomputeKey` mientras cargan varias hojas) llega al interceptor con
 * `error.response` vacío — el MISMO shape que un error de red real (DNS,
 * timeout, CORS bloqueado). Antes de este fix, `handleApiResponseError`
 * reportaba las dos por igual a Sentry vía `captureException`, así que un
 * comportamiento normal y esperado (descartar lo obsoleto) aparecía como un
 * error real en el monitoreo. Ahora solo se reporta el error de red genuino.
 */

const mockCaptureException = jest.fn();
const mockAddBreadcrumb = jest.fn();

jest.mock("@sentry/nextjs", () => ({
  captureException: (...args: unknown[]) => mockCaptureException(...args),
  addBreadcrumb: (...args: unknown[]) => mockAddBreadcrumb(...args),
}));

import { handleApiResponseError } from "@/lib/api";

function sinRespuesta(overrides: Partial<AxiosError> = {}): AxiosError {
  return {
    isAxiosError: true,
    config: { method: "post", url: "/ingestion/files/x/inventory-effects", headers: {} },
    response: undefined,
    toJSON: () => ({}),
    name: "AxiosError",
    message: "Network Error",
    ...overrides,
  } as unknown as AxiosError;
}

beforeEach(() => {
  jest.clearAllMocks();
});

test("una request cancelada NO se reporta como excepción a Sentry", async () => {
  const error = sinRespuesta({
    name: "CanceledError",
    message: "canceled",
    // axios.isCancel() mira exactamente este flag.
    ...({ __CANCEL__: true } as Partial<AxiosError>),
  });

  await expect(handleApiResponseError(error)).rejects.toBe(error);

  expect(mockCaptureException).not.toHaveBeenCalled();
  expect(mockAddBreadcrumb).toHaveBeenCalledWith(
    expect.objectContaining({ level: "info", message: expect.stringContaining("canceled") }),
  );
});

test("un error de red real (sin cancelar) SÍ se reporta a Sentry", async () => {
  const error = sinRespuesta();

  await expect(handleApiResponseError(error)).rejects.toBe(error);

  expect(mockCaptureException).toHaveBeenCalledWith(error, expect.any(Object));
});
