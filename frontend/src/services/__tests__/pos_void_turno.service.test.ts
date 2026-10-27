/**
 * A-4: la anulación manda el turno abierto de la pantalla. Si el ticket es de
 * otro turno u otro día, el reintegro sale de ESE cajón; sin decirlo, con dos
 * turnos abiertos en la caja el servidor responde 409. Se prueba con el `api`
 * real y un adapter de axios que hace de servidor: se ve el cuerpo del POST.
 */
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";
import { api } from "@/lib/api";
import { posService } from "../pos.service";

const pedidos: { url?: string; body: unknown }[] = [];
const adapterOriginal = api.defaults.adapter;

beforeEach(() => {
  pedidos.length = 0;
  api.defaults.adapter = (async (config: InternalAxiosRequestConfig) => {
    pedidos.push({
      url: config.url,
      body:
        typeof config.data === "string" ? JSON.parse(config.data) : config.data,
    });
    return {
      data: { id: "op1", status: "VOIDED" },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  }) as AxiosAdapter;
});

afterAll(() => {
  api.defaults.adapter = adapterOriginal;
});

describe("voidOperation (A-4)", () => {
  it("manda el turno abierto de la pantalla", async () => {
    await posService.voidOperation("op1", "lo devolvió", "turno-hoy");
    expect(pedidos).toEqual([
      {
        url: "/pos/operations/op1/void",
        body: { reason: "lo devolvió", client_session_id: "turno-hoy" },
      },
    ]);
  });

  it("sin turno no inventa uno", async () => {
    await posService.voidOperation("op1", "lo devolvió", null);
    expect(pedidos[0]?.body).toEqual({ reason: "lo devolvió" });
  });
});
