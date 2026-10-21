/**
 * B·5e: «no se puede vender algo que no se compró». El 422 trae en
 * `detail.message` de qué producto falta la compra; la pantalla lo muestra en vez
 * del genérico.
 */
import { AxiosError, AxiosHeaders } from "axios";

import { mensajeDelServidor } from "../useOfflineSubmit";

function error422(data: unknown): AxiosError {
  return new AxiosError("422", "ERR_BAD_REQUEST", undefined, undefined, {
    status: 422,
    statusText: "Unprocessable",
    headers: {},
    config: { headers: new AxiosHeaders() },
    data,
  });
}

test("toma el message del detail estructurado", () => {
  const e = error422({
    detail: { code: "SALE_WITHOUT_PURCHASE", message: "No hay compra ni stock inicial de «Vela»." },
  });
  expect(mensajeDelServidor(e)).toBe("No hay compra ni stock inicial de «Vela».");
});

test("sin message legible devuelve undefined (queda el genérico)", () => {
  expect(mensajeDelServidor(error422({ detail: [{ loc: ["body"], msg: "x" }] }))).toBeUndefined();
  expect(mensajeDelServidor(error422({ detail: "texto" }))).toBeUndefined();
  expect(mensajeDelServidor(new Error("x"))).toBeUndefined();
});
