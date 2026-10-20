import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { MarginCard } from "@/features/dashboard/DashboardSummaryCards";
import { fetchMargins } from "@/services/dashboard.service";
import type { MarginFact, MarginsResponse } from "@/types/api";

// M·3-2: la tarjeta pinta lo que manda FactsService; no calcula nada.
jest.mock("next/navigation", () => ({ useRouter: () => ({ push: jest.fn() }) }));
jest.mock("@/services/dashboard.service", () => ({ fetchMargins: jest.fn() }));
const mockFetch = fetchMargins as jest.Mock;

function fact(over: Partial<MarginFact>): MarginFact {
  return {
    value: 25, estado: "confirmado", motivo: null, periodo: "mes_2026_09",
    desde: "2026-09-01", hasta: "2026-09-30", cobertura_confirmada_pct: 90,
    cobertura_total_pct: 90, ingreso_ars: 1000, costo_vendido_ars: 600,
    gastos_operativos_ars: 150, resultado_ars: 250, categoria: null, ...over,
  };
}

function respuesta(over: Partial<MarginsResponse>): MarginsResponse {
  return {
    sin_datos: false, motivo: null, granularidad: "dia",
    neto_mes_cerrado: fact({}),
    bruto_mes_en_curso: fact({ value: 38.1, desde: "2026-10-01", hasta: "2026-10-17", periodo: "mes_en_curso_2026_10" }),
    bruto_periodo: null, serie_bruto: [], serie_neto: [], por_categoria: [], ...over,
  };
}

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MarginCard />
    </QueryClientProvider>,
  );
}

describe("MarginCard", () => {
  it("neto del mes cerrado arriba, bruto en curso debajo, con categorías", async () => {
    mockFetch.mockResolvedValue(respuesta({
      por_categoria: [fact({ categoria: "Bebidas", value: 50 }), fact({ categoria: "Sin producto", value: null, estado: "ausente", motivo: "cobertura de costo 0,0%" })],
    }));
    renderCard();
    expect(await screen.findByText("Margen neto · septiembre 2026")).toBeInTheDocument();
    expect(screen.getByText("25,0%")).toBeInTheDocument();
    expect(screen.getByText("Margen bruto · octubre 2026 (en curso)")).toBeInTheDocument();
    expect(screen.getByText("38,1%")).toBeInTheDocument();
    expect(screen.getByText("Bebidas")).toBeInTheDocument();
    expect(screen.getByText("Sin producto")).toBeInTheDocument();
  });

  it("neto ausente: «—» con el motivo, nunca 0", async () => {
    mockFetch.mockResolvedValue(respuesta({
      neto_mes_cerrado: fact({ value: null, estado: "ausente", motivo: "sin gastos cargados en el mes" }),
    }));
    renderCard();
    expect(await screen.findByText("sin gastos cargados en el mes")).toBeInTheDocument();
    expect(screen.queryByText("0,0%")).not.toBeInTheDocument();
  });

  it("sin datos (completitud < 50): estado vacío", async () => {
    mockFetch.mockResolvedValue(respuesta({ sin_datos: true, motivo: "faltan datos del negocio para medir el margen", neto_mes_cerrado: null, bruto_mes_en_curso: null }));
    renderCard();
    expect(await screen.findByText("Todavía no hay datos para medir el margen")).toBeInTheDocument();
  });

  it("M·2d-3: los retiros que superan el resultado salen como dato, no como alerta", async () => {
    mockFetch.mockResolvedValue(respuesta({
      retiros_vs_resultado: {
        periodo: "mes_2026_09", desde: "2026-09-01", hasta: "2026-09-30",
        retiros_ars: 5500, resultado_ars: 5000, estimado: false,
      },
    }));
    renderCard();
    const linea = await screen.findByText(/retiraste/);
    expect(linea.textContent?.replace(/\s+/g, " ")).toMatch(
      /^En septiembre 2026 retiraste \$\s?5\.500 y el negocio dejó \$\s?5\.000\.$/,
    );
    expect(linea.className).not.toMatch(/red|danger|amber/);
  });

  it("M·2d-3: sobre un neto estimado, el resultado dice «aprox.»", async () => {
    mockFetch.mockResolvedValue(respuesta({
      retiros_vs_resultado: {
        periodo: "mes_2026_09", desde: "2026-09-01", hasta: "2026-09-30",
        retiros_ars: 100, resultado_ars: -2000, estimado: true,
      },
    }));
    renderCard();
    // Resultado negativo: «perdió», sin un «dejó -$».
    expect((await screen.findByText(/retiraste/)).textContent?.replace(/\s+/g, " ")).toMatch(
      /el negocio perdió aprox\. \$\s?2\.000\.$/,
    );
  });

  it("M·2d-3: sin señal no hay línea de retiros", async () => {
    mockFetch.mockResolvedValue(respuesta({}));
    renderCard();
    await screen.findByText("Margen neto · septiembre 2026");
    expect(screen.queryByText(/retiraste/)).not.toBeInTheDocument();
  });

  it("error: lo dice, sin números", async () => {
    mockFetch.mockRejectedValue(new Error("500"));
    renderCard();
    expect(await screen.findByText("No se pudo leer el margen ahora.")).toBeInTheDocument();
  });
});
