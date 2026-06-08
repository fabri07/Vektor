import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ColumnMapperPanel } from "../ColumnMapperPanel";
import { ingestionService } from "@/services/ingestion.service";

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    getPreview: jest.fn(),
    getColumnMappings: jest.fn(),
    confirmFile: jest.fn(),
    cancelFile: jest.fn(),
  },
}));

const mockGetPreview = ingestionService.getPreview as jest.Mock;
const mockGetColumnMappings = ingestionService.getColumnMappings as jest.Mock;

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ColumnMapperPanel fileId="file-1" onDone={jest.fn()} />
    </QueryClientProvider>,
  );
}

describe("ColumnMapperPanel — A3 clarificación inline", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetColumnMappings.mockResolvedValue([]);
  });

  test("archivo ambiguo (general) muestra el selector de propósito", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "general", headers: ["a", "b"] },
      columns_at_risk: [],
    });

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Revisá antes de confirmar/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/No pudimos determinar/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ventas" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Gastos" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Productos/i })).toBeInTheDocument();
    // No se piden mapeos hasta elegir el propósito.
    expect(mockGetColumnMappings).not.toHaveBeenCalled();
  });

  test("renderiza columns_at_risk del preview", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["fecha", "obs"] },
      columns_at_risk: [
        { column: "obs", null_pct: 72, recommendation: "Revisá o eliminá la columna" },
      ],
    });

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Columnas con muchos datos vacíos/i)).toBeInTheDocument();
    });
    expect(screen.getByText("obs")).toBeInTheDocument();
    expect(screen.getByText(/72% vacío/i)).toBeInTheDocument();
  });

  test("muestra el source 'llm' como IA con su confianza", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["ColX"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.8,
        source: "llm",
        status: "mapped",
      },
    ]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/IA/)).toBeInTheDocument();
    });
    expect(screen.getByText(/80%/)).toBeInTheDocument();
  });
});
