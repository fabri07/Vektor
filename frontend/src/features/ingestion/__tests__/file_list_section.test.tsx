import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { FileListSection } from "../FileListSection";
import { ingestionService, type UploadedFileItem } from "@/services/ingestion.service";

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: jest.fn() }),
}));

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    listFiles: jest.fn(),
    deleteFile: jest.fn(),
    reprocessFile: jest.fn(),
  },
}));

const mockListFiles = ingestionService.listFiles as jest.Mock;

function fileWith(status: string): UploadedFileItem {
  return {
    id: "file-1",
    original_filename: "ventas.csv",
    content_type: "text/csv",
    size_bytes: 100,
    purpose: "ingestion",
    processing_status: status,
    created_at: "2026-07-19T10:00:00Z",
  };
}

function renderList() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <FileListSection />
    </QueryClientProvider>,
  );
}

describe("FileListSection — estado IMPORTING", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("fila IMPORTING muestra indicador indeterminado y aviso, sin botón Eliminar", async () => {
    mockListFiles.mockResolvedValue([fileWith("IMPORTING")]);

    renderList();

    // Pill de estado + barra indeterminada con su aviso honesto.
    await waitFor(() => {
      expect(screen.getByText("Importando…")).toBeInTheDocument();
    });
    expect(
      screen.getByText(/no cierres esta ventana mientras termina/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();

    // Eliminar oculto mientras corre el import.
    expect(
      screen.queryByTitle("Eliminar archivo"),
    ).not.toBeInTheDocument();
  });

  test("fila DONE sí muestra el botón Eliminar (control)", async () => {
    mockListFiles.mockResolvedValue([fileWith("DONE")]);

    renderList();

    await waitFor(() => {
      expect(screen.getByText("Importado")).toBeInTheDocument();
    });
    expect(screen.getByTitle("Eliminar archivo")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});
