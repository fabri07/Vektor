/**
 * `reclassify` / `link-product` de «Otros» tardan varios segundos y el servidor
 * termina y guarda aunque el navegador corte: con el timeout de 15 s el usuario veía
 * un error con la venta ya registrada (pruebas del dueño, 2026-11-16).
 */
jest.mock("@sentry/nextjs", () => ({ captureException: jest.fn(), addBreadcrumb: jest.fn() }));

import { api, esEscrituraDeOtros, TIMEOUT_ESCRITURA_IDEMPOTENTE_MS } from "@/lib/api";

type Interceptor = { fulfilled: (c: Record<string, unknown>) => Record<string, unknown> };

function pasarPorElInterceptor(method: string, url: string): Record<string, unknown> {
  const handlers = (api.interceptors.request as unknown as { handlers: Interceptor[] }).handlers;
  const config = { method, url, headers: {} as Record<string, unknown>, timeout: 15_000 };
  return handlers.reduce((c, h) => (h ? h.fulfilled(c) : c), config as Record<string, unknown>);
}

test("reconoce sólo reclassify y link-product de una fila", () => {
  expect(esEscrituraDeOtros("/others/abc-123/reclassify")).toBe(true);
  expect(esEscrituraDeOtros("/others/abc-123/link-product")).toBe(true);
  expect(esEscrituraDeOtros("/others/abc-123/dismiss")).toBe(false);
  expect(esEscrituraDeOtros("/others")).toBe(false);
  expect(esEscrituraDeOtros(undefined)).toBe(false);
});

test("el POST de reclassify y link-product espera lo mismo que una escritura idempotente", () => {
  expect(pasarPorElInterceptor("post", "/others/r1/reclassify").timeout).toBe(
    TIMEOUT_ESCRITURA_IDEMPOTENTE_MS,
  );
  expect(pasarPorElInterceptor("post", "/others/r1/link-product").timeout).toBe(
    TIMEOUT_ESCRITURA_IDEMPOTENTE_MS,
  );
});

test("el resto de las requests conserva los 15 s", () => {
  expect(pasarPorElInterceptor("post", "/others/r1/dismiss").timeout).toBe(15_000);
  expect(pasarPorElInterceptor("get", "/others/r1/reclassify").timeout).toBe(15_000);
});
