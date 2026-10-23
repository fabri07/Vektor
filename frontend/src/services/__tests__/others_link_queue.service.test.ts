/**
 * B·8b: qué trae `getPendingSalesWithReference` por la red. El backend no filtra
 * por destino, así que la paginación, el filtro (venta con producto), la
 * deduplicación y el tope viven en el cliente. Se prueba con el `api` real y un
 * adapter de axios que hace de servidor: se ve la URL y los params de cada GET.
 */
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";
import { api } from "@/lib/api";
import { othersService } from "../others.service";

type Fila = { id: string; suggested_entity: string | null; row_data: Record<string, string> };

let servidor: (offset: number, limit: number) => Fila[];
const pedidos: { url?: string; params: Record<string, unknown> }[] = [];
const adapterOriginal = api.defaults.adapter;

beforeEach(() => {
  pedidos.length = 0;
  api.defaults.adapter = (async (config: InternalAxiosRequestConfig) => {
    const params = (config.params ?? {}) as Record<string, number | string>;
    pedidos.push({ url: config.url, params });
    return {
      data: servidor(Number(params.offset), Number(params.limit)),
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

const venta = (id: string, producto = "Coca 500"): Fila => ({
  id,
  suggested_entity: "sale",
  row_data: { producto },
});

it("pagina de a 500 hasta una página corta y filtra ventas con producto", async () => {
  const todas: Fila[] = [];
  for (let i = 0; i < 700; i++) {
    if (i % 7 === 0) todas.push({ id: `g${i}`, suggested_entity: "expense", row_data: { producto: "x" } });
    else if (i % 7 === 1) todas.push(venta(`s${i}`, "  "));
    else todas.push(venta(`v${i}`));
  }
  servidor = (offset, limit) => todas.slice(offset, offset + limit);

  const { records, truncado } = await othersService.getPendingSalesWithReference(5000);

  expect(pedidos.map((p) => [p.url, p.params])).toEqual([
    ["/others", { status: "PENDING", limit: 500, offset: 0 }],
    ["/others", { status: "PENDING", limit: 500, offset: 500 }],
  ]);
  expect(truncado).toBe(false);
  expect(records).toHaveLength(500);
  expect(records.every((r) => r.id.startsWith("v"))).toBe(true);
});

it("deduplica una venta que llega en dos páginas", async () => {
  // Se resolvió una fila entre página y página: el offset corrió una.
  const p1 = Array.from({ length: 500 }, (_, i) => venta(`v${i}`));
  const p2 = [venta("v499"), venta("v500")];
  servidor = (offset) => (offset === 0 ? p1 : p2);

  const { records } = await othersService.getPendingSalesWithReference(5000);

  expect(records).toHaveLength(501);
  expect(new Set(records.map((r) => r.id)).size).toBe(501);
});

it("corta en el tope de ventas y lo dice", async () => {
  servidor = (offset, limit) =>
    Array.from({ length: limit }, (_, i) => venta(`v${offset + i}`));

  const { records, truncado } = await othersService.getPendingSalesWithReference(5000);

  expect(records).toHaveLength(5000);
  expect(truncado).toBe(true);
  // 5.000 ventas son 10 páginas; la 11.ª muestra que había más.
  expect(pedidos).toHaveLength(11);
});
