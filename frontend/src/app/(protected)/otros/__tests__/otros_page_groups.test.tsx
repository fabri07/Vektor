import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import OtrosPage from "../page";
import { othersService } from "@/services/others.service";
import { productsService } from "@/services/products.service";

const mockAddToast = jest.fn();
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: mockAddToast }),
}));

jest.mock("@/services/others.service", () => ({
  othersService: {
    getPending: jest.fn(),
    getPendingCount: jest.fn(),
    getSummary: jest.fn(),
    dismissGroup: jest.fn(),
    dismiss: jest.fn(),
    bulkImport: jest.fn(),
    reclassify: jest.fn(),
    resolvePurchase: jest.fn(),
  },
}));

jest.mock("@/services/products.service", () => ({
  productsService: { getCategories: jest.fn() },
}));

const getSummaryMock = othersService.getSummary as jest.Mock;
const getPendingMock = othersService.getPending as jest.Mock;
const getPendingCountMock = othersService.getPendingCount as jest.Mock;
const dismissGroupMock = othersService.dismissGroup as jest.Mock;
const getCategoriesMock = productsService.getCategories as jest.Mock;

const GROUPS = [
  {
    uploaded_file_id: "file-1",
    original_filename: "Ganancias.xlsx",
    source: "ingestion" as const,
    context_label: "Ganancias",
    suggested_entity: null,
    status: "PENDING" as const,
    count: 1840,
  },
  {
    uploaded_file_id: "file-2",
    original_filename: "libro_diario.xlsx",
    source: "ingestion" as const,
    context_label: "Movimientos ambiguos",
    suggested_entity: null,
    status: "PENDING" as const,
    count: 4,
  },
];

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OtrosPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  getSummaryMock.mockResolvedValue(GROUPS);
  getPendingMock.mockResolvedValue([]);
  getPendingCountMock.mockResolvedValue(1844);
  getCategoriesMock.mockResolvedValue([]);
});

describe("OtrosPage — vista de grupos (F-O.3)", () => {
  it("arranca mostrando los grupos agrupados, no las 1844 filas sueltas", async () => {
    renderPage();

    expect(await screen.findByText("Ganancias")).toBeInTheDocument();
    expect(screen.getByText("Movimientos ambiguos")).toBeInTheDocument();
    expect(screen.getByText("1840")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    // La lista plana de filas NO se pidió al arrancar en la vista de grupos.
    expect(getPendingMock).not.toHaveBeenCalled();
  });

  it("«Ver filas» de un grupo pasa a la lista filtrada por ese grupo", async () => {
    renderPage();
    await screen.findByText("Ganancias");

    const row = screen.getByText("Ganancias").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /ver filas/i }));

    await waitFor(() =>
      expect(getPendingMock).toHaveBeenCalledWith(
        0,
        50,
        expect.objectContaining({
          uploaded_file_id: "file-1",
          context_label: "Ganancias",
        }),
      ),
    );
    expect(await screen.findByText(/Ganancias\.xlsx/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /volver a grupos/i })).toBeInTheDocument();
  });

  it("«Volver a grupos» limpia el filtro y vuelve a la vista agrupada", async () => {
    renderPage();
    await screen.findByText("Ganancias");
    const row = screen.getByText("Ganancias").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /ver filas/i }));
    await screen.findByRole("button", { name: /volver a grupos/i });

    fireEvent.click(screen.getByRole("button", { name: /volver a grupos/i }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /volver a grupos/i })).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("Movimientos ambiguos")).toBeInTheDocument();
  });

  it("descarta el grupo entero con el conteo visto y vuelve a grupos", async () => {
    jest.spyOn(window, "confirm").mockReturnValue(true);
    dismissGroupMock.mockResolvedValue(1840);
    renderPage();
    await screen.findByText("Ganancias");

    const row = screen.getByText("Ganancias").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /descartar todo el grupo/i }));

    await waitFor(() =>
      expect(dismissGroupMock).toHaveBeenCalledWith(
        expect.objectContaining({
          uploaded_file_id: "file-1",
          context_label: "Ganancias",
          expected_count: 1840,
        }),
      ),
    );
    expect(mockAddToast).toHaveBeenCalledWith("1840 registro(s) descartados.", "success");
  });

  it("no descarta si el usuario cancela la confirmación", async () => {
    jest.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    await screen.findByText("Ganancias");

    const row = screen.getByText("Ganancias").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /descartar todo el grupo/i }));

    expect(dismissGroupMock).not.toHaveBeenCalled();
  });

  it("si el grupo cambió desde el snapshot (409), avisa y refresca — no reintenta a ciegas", async () => {
    jest.spyOn(window, "confirm").mockReturnValue(true);
    dismissGroupMock.mockRejectedValue({
      response: { status: 409, data: { detail: { code: "GROUP_CHANGED" } } },
    });
    renderPage();
    await screen.findByText("Ganancias");

    const row = screen.getByText("Ganancias").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /descartar todo el grupo/i }));

    await waitFor(() =>
      expect(mockAddToast).toHaveBeenCalledWith(
        expect.stringContaining("cambió"),
        "info",
      ),
    );
  });
});
