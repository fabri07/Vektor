import type { MarginFact, MarginsResponse } from "@/types/api";

/**
 * M·3-2: cómo se dice un margen en pantalla. Sólo formatea lo que manda
 * FactsService (`GET /insights/margins`): acá no se calcula ningún margen.
 */

const MESES = [
  "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre",
];

/** «septiembre 2026» desde `desde` (AAAA-MM-DD). */
export function nombreDelMes(fact: Pick<MarginFact, "desde">): string {
  const desde = fact.desde ?? "";
  const anio = Number(desde.slice(0, 4));
  const mes = Number(desde.slice(5, 7));
  if (!anio || !mes) return "el período";
  return `${MESES[mes - 1]} ${anio}`;
}

export function formatPct(value: number): string {
  return `${value.toFixed(1).replace(".", ",")}%`;
}

export type MarginDisplay = {
  /** «25,0%» o «—». */
  valor: string;
  /** «estimado», «sin medir» o null si es confirmado. */
  rotulo: string | null;
  /** Motivo y cobertura, para el texto chico. */
  detalle: string | null;
  tono: "negativo" | "normal" | "ausente";
};

export function describirMargen(fact: MarginFact | null | undefined): MarginDisplay {
  if (!fact || fact.value == null) {
    return {
      valor: "—",
      rotulo: "sin medir",
      detalle: fact?.motivo ?? "sin ventas en el período",
      tono: "ausente",
    };
  }
  const cobertura =
    fact.cobertura_total_pct != null ? `cobertura de costo ${formatPct(fact.cobertura_total_pct)}` : null;
  const estimado = fact.estado === "estimado";
  return {
    valor: formatPct(fact.value),
    rotulo: estimado ? "estimado" : null,
    detalle: [estimado ? fact.motivo : null, cobertura].filter(Boolean).join(" · ") || null,
    tono: fact.value < 0 ? "negativo" : "normal",
  };
}

/**
 * Por qué no hay serie de margen para mostrar (o `null` si la hay). El neto es
 * por mes CERRADO: con un período dentro del mes en curso no tiene puntos.
 */
export function avisoDeSerieDeMargen(
  metric: "margen" | "margen_neto",
  data: MarginsResponse | undefined,
  isError: boolean,
): string | null {
  if (isError) return "No se pudo leer el margen ahora. Probá con otro período.";
  if (!data) return null;
  if (data.sin_datos) return data.motivo ?? "Todavía no hay datos para medir el margen.";
  if (metric === "margen_neto") {
    return data.serie_neto.length === 0
      ? "El margen neto se mide por mes cerrado: elegí un período que incluya meses ya terminados."
      : null;
  }
  return data.serie_bruto.some((f) => f.value != null)
    ? null
    : "No hay margen bruto medible en el período (sin ventas o sin costo suficiente).";
}

/** Etiqueta del eje para un punto de la serie (día, semana o mes). */
export function etiquetaDelTramo(fact: MarginFact, granularidad: "dia" | "semana" | "mes"): string {
  const desde = fact.desde ?? "";
  if (granularidad === "mes") return nombreDelMes(fact);
  const [, mes, dia] = desde.split("-");
  return granularidad === "semana" ? `sem. ${dia}/${mes}` : `${dia}/${mes}`;
}
