"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LineChart } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Tooltip } from "@/components/ui/Tooltip";

interface EconomicTickerItem {
  label: string;
  value: string;
  change?: string;
  changeType?: "up" | "down" | "neutral";
}

const TOOLTIP_COPY: Record<string, string> = {
  "💵 Oficial": "Dólar Oficial: el precio del dólar en bancos y casas de cambio autorizadas.",
  "🔵 Blue": "Dólar Blue: el precio del dólar en el mercado informal.",
  "📈 MEP": "Dólar MEP: el tipo de cambio implícito al comprar y vender bonos dentro del mercado local.",
  "🌐 CCL": "Dólar CCL: el tipo de cambio implícito usado para girar capital al exterior mediante bonos.",
  "📊 Inflación (mar)": "Inflación mensual: cuánto subieron en promedio los precios durante el último mes reportado.",
  "🔮 REM anual": "REM anual: proyección de inflación a 12 meses elaborada por analistas relevados por el BCRA.",
  "🏦 Tasa BCRA": "Tasa BCRA: referencia del costo del dinero en pesos que influye sobre créditos, plazos fijos y financiamiento.",
};

function toneClass(label: string) {
  if (label.includes("Inflación")) return "text-vektor-amber";
  if (label.includes("Tasa")) return "text-vektor-blue";
  return "text-vektor-body";
}

function TickerItem({ item }: { item: EconomicTickerItem }) {
  return (
    <Tooltip content={TOOLTIP_COPY[item.label] ?? item.label}>
      <div className="inline-flex items-center gap-1.5 px-4 text-xs font-medium">
        <span className={toneClass(item.label)}>{item.label}:</span>
        <span className="text-vektor-white">{item.value}</span>
        {item.change ? (
          <span
            className={
              item.changeType === "up"
                ? "text-vektor-teal"
                : item.changeType === "down"
                  ? "text-vektor-red"
                  : "text-vektor-muted"
            }
          >
            {item.change}
          </span>
        ) : null}
      </div>
    </Tooltip>
  );
}

function TickerContent({ items }: { items: EconomicTickerItem[] }) {
  const marqueeItems = [...items, ...items];

  return (
    <div className="group relative hidden h-9 overflow-hidden border-b border-vektor-border bg-vektor-ink sm:block">
      <div className="animate-marquee flex min-w-max items-center group-hover:[animation-play-state:paused]">
        {marqueeItems.map((item, index) => (
          <div key={`${item.label}-${index}`} className="flex items-center">
            <TickerItem item={item} />
            <span className="text-vektor-muted">·</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function EconomicTicker() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { data = [], isLoading } = useQuery<EconomicTickerItem[]>({
    queryKey: ["economia-ticker"],
    queryFn: async () => {
      const response = await fetch("/api/economia");
      if (!response.ok) {
        throw new Error("No se pudo cargar el ticker económico.");
      }
      return (await response.json()) as EconomicTickerItem[];
    },
    staleTime: 30 * 60 * 1000,
  });

  const items = data.length > 0
    ? data
    : [
        { label: "💵 Oficial", value: "$ 1.245" },
        { label: "🔵 Blue", value: "$ 1.380" },
        { label: "📈 MEP", value: "$ 1.362" },
        { label: "🌐 CCL", value: "$ 1.370" },
        { label: "📊 Inflación (mar)", value: "3.7%" },
        { label: "🔮 REM anual", value: "38.5%" },
        { label: "🏦 Tasa BCRA", value: "29%" },
      ];

  return (
    <>
      <TickerContent items={items} />

      <div className="border-b border-vektor-border bg-vektor-ink px-4 py-2 sm:hidden">
        <button
          type="button"
          onClick={() => setMobileOpen(true)}
          aria-label="Abrir información del mercado"
          className="inline-flex items-center gap-2 rounded-full border border-vektor-border bg-vektor-surface px-3 py-1 text-xs font-medium text-vektor-body"
        >
          <LineChart className="h-3.5 w-3.5 text-vektor-blue" />
          {isLoading ? "Cargando mercado..." : "📊 Mercado"}
        </button>
      </div>

      <Modal
        isOpen={mobileOpen}
        onClose={() => setMobileOpen(false)}
        title="Mercado"
        size="md"
      >
        <div className="space-y-3">
          {items.map((item) => (
            <div
              key={item.label}
              className="rounded-xl border border-vektor-border bg-vektor-surface p-3"
            >
              <p className={`text-sm font-medium ${toneClass(item.label)}`}>{item.label}</p>
              <p className="mt-1 text-lg font-semibold text-vektor-white">{item.value}</p>
              <p className="mt-1 text-xs text-vektor-muted">
                {TOOLTIP_COPY[item.label] ?? item.label}
              </p>
            </div>
          ))}
        </div>
      </Modal>
    </>
  );
}
