import { NextResponse } from "next/server";

// Revalidación cada 30 min: las fuentes son públicas y se actualizan a diario
// (dólar/riesgo país) o mensualmente (inflación). Antes los índices estaban
// hardcodeados ("Inflación mar 3.7%") y quedaban viejos — ahora son en vivo.
export const revalidate = 1800;

interface DollarApiQuote {
  compra: number;
  venta: number;
  casa: string;
  nombre: string;
  moneda: string;
  fechaActualizacion: string;
}

interface EconomicTickerItem {
  label: string;
  value: string;
  change?: string;
  changeType?: "up" | "down" | "neutral";
}

interface DatedValue {
  fecha: string;
  valor: number;
}

const MONTHS_ES = [
  "ene", "feb", "mar", "abr", "may", "jun",
  "jul", "ago", "sep", "oct", "nov", "dic",
];

// Mes abreviado desde "YYYY-MM-DD" sin pasar por Date (evita corrimientos de TZ).
function monthAbbr(fecha: string): string {
  const month = Number.parseInt(fecha.slice(5, 7), 10);
  return MONTHS_ES[month - 1] ?? "";
}

function formatPeso(value: number | null | undefined): string {
  if (!Number.isFinite(value)) return "$ —";
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value ?? 0);
}

function getQuote(quotes: DollarApiQuote[], casa: string): DollarApiQuote | undefined {
  return quotes.find((quote) => quote.casa === casa);
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, {
    next: { revalidate: 1800 },
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return (await res.json()) as T;
}

async function getDollarItems(): Promise<EconomicTickerItem[]> {
  try {
    const quotes = await fetchJson<DollarApiQuote[]>("https://dolarapi.com/v1/dolares");
    return [
      { label: "💵 Oficial", value: formatPeso(getQuote(quotes, "oficial")?.venta) },
      { label: "🔵 Blue", value: formatPeso(getQuote(quotes, "blue")?.venta) },
      { label: "📈 MEP", value: formatPeso(getQuote(quotes, "bolsa")?.venta) },
      { label: "🌐 CCL", value: formatPeso(getQuote(quotes, "contadoconliqui")?.venta) },
    ];
  } catch {
    return [
      { label: "💵 Oficial", value: "$ —" },
      { label: "🔵 Blue", value: "$ —" },
      { label: "📈 MEP", value: "$ —" },
      { label: "🌐 CCL", value: "$ —" },
    ];
  }
}

async function getInflationMonthly(): Promise<EconomicTickerItem> {
  try {
    const series = await fetchJson<DatedValue[]>(
      "https://api.argentinadatos.com/v1/finanzas/indices/inflacion",
    );
    const last = series.at(-1);
    if (!last) throw new Error("serie vacía");
    return {
      label: `📊 Inflación (${monthAbbr(last.fecha)})`,
      value: `${last.valor.toFixed(1)}%`,
      changeType: "neutral",
    };
  } catch {
    return { label: "📊 Inflación (mensual)", value: "—", changeType: "neutral" };
  }
}

async function getInflationYoY(): Promise<EconomicTickerItem> {
  try {
    const series = await fetchJson<DatedValue[]>(
      "https://api.argentinadatos.com/v1/finanzas/indices/inflacionInteranual",
    );
    const last = series.at(-1);
    if (!last) throw new Error("serie vacía");
    return {
      label: "📈 Inflación i.a.",
      value: `${last.valor.toFixed(1)}%`,
      changeType: "neutral",
    };
  } catch {
    return { label: "📈 Inflación i.a.", value: "—", changeType: "neutral" };
  }
}

async function getCountryRisk(): Promise<EconomicTickerItem> {
  try {
    const data = await fetchJson<DatedValue>(
      "https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais/ultimo",
    );
    return {
      label: "📉 Riesgo país",
      value: `${Math.round(data.valor)}`,
      changeType: "neutral",
    };
  } catch {
    return { label: "📉 Riesgo país", value: "—", changeType: "neutral" };
  }
}

export async function GET() {
  // Cada fuente con su propio fallback: una caída no tira abajo el ticker entero.
  const [dollarItems, inflationMonthly, inflationYoY, countryRisk] = await Promise.all([
    getDollarItems(),
    getInflationMonthly(),
    getInflationYoY(),
    getCountryRisk(),
  ]);

  return NextResponse.json([
    ...dollarItems,
    inflationMonthly,
    inflationYoY,
    countryRisk,
  ]);
}
