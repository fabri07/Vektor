import { NextResponse } from "next/server";

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

const FALLBACK_ITEMS: EconomicTickerItem[] = [
  { label: "📊 Inflación (mar)", value: "3.7%", changeType: "neutral" },
  { label: "🔮 REM anual", value: "38.5%", changeType: "neutral" },
  { label: "🏦 Tasa BCRA", value: "29%", changeType: "neutral" },
];

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

export async function GET() {
  let dollarItems: EconomicTickerItem[] = [];

  try {
    const response = await fetch("https://dolarapi.com/v1/dolares", {
      next: { revalidate: 1800 },
      headers: {
        Accept: "application/json",
      },
    });

    if (!response.ok) {
      throw new Error(`DolarApi error ${response.status}`);
    }

    const quotes = (await response.json()) as DollarApiQuote[];
    const official = getQuote(quotes, "oficial");
    const blue = getQuote(quotes, "blue");
    const mep = getQuote(quotes, "bolsa");
    const ccl = getQuote(quotes, "contadoconliqui");

    dollarItems = [
      { label: "💵 Oficial", value: formatPeso(official?.venta) },
      { label: "🔵 Blue", value: formatPeso(blue?.venta) },
      { label: "📈 MEP", value: formatPeso(mep?.venta) },
      { label: "🌐 CCL", value: formatPeso(ccl?.venta) },
    ];
  } catch {
    dollarItems = [
      { label: "💵 Oficial", value: "$ 1.245" },
      { label: "🔵 Blue", value: "$ 1.380" },
      { label: "📈 MEP", value: "$ 1.362" },
      { label: "🌐 CCL", value: "$ 1.370" },
    ];
  }

  // TODO: Replace with BCRA API / robust parser once the source format is stable.
  const bcraItems = FALLBACK_ITEMS;

  return NextResponse.json([...dollarItems, ...bcraItems]);
}
