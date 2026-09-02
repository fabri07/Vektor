import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import OtrosPage from "../page";
import { othersService } from "@/services/others.service";
import { productsService } from "@/services/products.service";

jest.mock("@/services/others.service", () => ({
  othersService: {
    getPending: jest.fn(),
    getPendingCounts: jest.fn(),
    reclassify: jest.fn(),
    dismiss: jest.fn(),
    bulkImport: jest.fn(),
    linkToProduct: jest.fn(),
    resolvePurchase: jest.fn(),
  },
}));

jest.mock("@/services/products.service", () => ({
  productsService: { getCategories: jest.fn() },
}));

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: jest.fn() }),
}));

const registro = (id: string, suggested: "sale" | "expense" | null) => ({
  id,
  uploaded_file_id: null,
  source: "ingestion",
  context_label: "Ganancias",
  headers: ["periodo", "total"],
  row_data: { periodo: "Enero", total: "999" },
  suggested_entity: suggested,
  suggested_category: null,
  suggested_category_label: null,
  match_candidates: null,
  status: "PENDING",
  created_at: "2026-08-10T10:00:00Z",
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OtrosPage />
    </QueryClientProvider>,
  );
}

describe('compuerta de "Importar todo lo sugerido"', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (productsService.getCategories as jest.Mock).mockResolvedValue([]);
  });

  it("lo deshabilita y explica por qué cuando ningún pendiente tiene destino sugerido", async () => {
    // El estado real de ASTERIA: miles de pendientes, cero sugerencias.
    (othersService.getPending as jest.Mock).mockResolvedValue([registro("r1", null)]);
    (othersService.getPendingCounts as jest.Mock).mockResolvedValue({
      pending: 2288,
      pendingSuggested: 0,
    });

    renderPage();

    const boton = await screen.findByRole("button", { name: /importar todo lo sugerido/i });
    await waitFor(() => expect(boton).toBeDisabled());
    // El motivo se ve, no vive sólo en el tooltip: un botón muerto sin
    // explicación es indistinguible de un bug.
    expect(
      screen.getByText(/ninguno tiene destino sugerido/i),
    ).toBeInTheDocument();
  });

  it("lo habilita cuando hay al menos una sugerencia, aunque no esté en esta página", async () => {
    // La página muestra 50 filas sin sugerencia; el conteo global dice que hay
    // 3 sugeridas más adelante. Decidir con la página se equivocaría.
    (othersService.getPending as jest.Mock).mockResolvedValue([registro("r1", null)]);
    (othersService.getPendingCounts as jest.Mock).mockResolvedValue({
      pending: 2288,
      pendingSuggested: 3,
    });

    renderPage();

    const boton = await screen.findByRole("button", { name: /importar todo lo sugerido/i });
    await waitFor(() => expect(boton).toBeEnabled());
    expect(screen.queryByText(/ninguno tiene destino sugerido/i)).not.toBeInTheDocument();
  });
});
