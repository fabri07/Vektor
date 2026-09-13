import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ImportReceiptModal } from "../ImportReceiptModal";
import { ingestionService, type FileReceiptResponse } from "@/services/ingestion.service";

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: { getFileReceipt: jest.fn() },
}));

const mockGetFileReceipt = ingestionService.getFileReceipt as jest.Mock;

function renderModal(fileId: string | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ImportReceiptModal fileId={fileId} filename="catalogo.xlsx" onClose={jest.fn()} />
    </QueryClientProvider>,
  );
}

const COLUMNA_GUARDADA = {
  context_id: "sheet:Catalogo",
  context_label: "Catálogo",
  source_column: "nombre",
  target_field: "name",
  result: "guardado" as const,
  reason: null,
  reason_kind: null,
  rows_affected: null,
};

beforeEach(() => {
  jest.clearAllMocks();
});

test("no pide nada mientras no hay archivo elegido", () => {
  renderModal(null);
  expect(mockGetFileReceipt).not.toHaveBeenCalled();
});

test("muestra la tabla de la última aplicación vigente", async () => {
  const receipt: FileReceiptResponse = {
    file_id: "f1",
    file_reverted: false,
    last_applied: {
      kind: "confirm",
      execution_id: "trace-1",
      applied_at: "2026-07-19T10:00:00Z",
      receipt: { version: 1, historical_incomplete: false, columns: [COLUMNA_GUARDADA] },
    },
    last_attempt: null,
  };
  mockGetFileReceipt.mockResolvedValue(receipt);

  renderModal("f1");

  expect(await screen.findByText("nombre")).toBeInTheDocument();
  expect(screen.getByText("Guardado")).toBeInTheDocument();
  expect(screen.queryByText(/eliminado/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/Último intento/i)).not.toBeInTheDocument();
});

test("un archivo eliminado avisa que la explicación se conserva igual", async () => {
  mockGetFileReceipt.mockResolvedValue({
    file_id: "f1",
    file_reverted: true,
    last_applied: {
      kind: "confirm",
      execution_id: "trace-1",
      applied_at: "2026-07-19T10:00:00Z",
      receipt: { version: 1, historical_incomplete: false, columns: [COLUMNA_GUARDADA] },
    },
    last_attempt: null,
  } satisfies FileReceiptResponse);

  renderModal("f1");

  expect(await screen.findByText(/fue eliminado/i)).toBeInTheDocument();
  // Pero la explicación sigue mostrándose — F11 no la borra.
  expect(screen.getByText("nombre")).toBeInTheDocument();
});

test("una relectura fallida posterior se ve aparte, sin tapar la última aplicación", async () => {
  mockGetFileReceipt.mockResolvedValue({
    file_id: "f1",
    file_reverted: false,
    last_applied: {
      kind: "confirm",
      execution_id: "trace-1",
      applied_at: "2026-07-19T10:00:00Z",
      receipt: { version: 1, historical_incomplete: false, columns: [COLUMNA_GUARDADA] },
    },
    last_attempt: {
      kind: "reread",
      execution_id: "run-1",
      status: "FAILED",
      at: "2026-07-20T10:00:00Z",
      error: "Producto↔Proveedor no habilitado",
      receipt: { version: 1, historical_incomplete: false, columns: [] },
    },
  } satisfies FileReceiptResponse);

  renderModal("f1");

  expect(await screen.findByText("nombre")).toBeInTheDocument();
  const encabezadoIntento = screen.getByText(/Último intento de relectura/i);
  expect(encabezadoIntento).toHaveTextContent("Falló");
  expect(screen.getByText("Producto↔Proveedor no habilitado")).toBeInTheDocument();
});

test("sin ninguna aplicación vigente, lo dice en vez de mostrar una tabla vacía engañosa", async () => {
  mockGetFileReceipt.mockResolvedValue({
    file_id: "f1",
    file_reverted: false,
    last_applied: null,
    last_attempt: null,
  } satisfies FileReceiptResponse);

  renderModal("f1");

  await waitFor(() =>
    expect(
      screen.getByText(/No hay ninguna ejecución aplicada vigente/i),
    ).toBeInTheDocument(),
  );
});

test("un comprobante histórico (pre-Cambio 4) se marca como reconstruido", async () => {
  mockGetFileReceipt.mockResolvedValue({
    file_id: "f1",
    file_reverted: false,
    last_applied: {
      kind: "confirm",
      execution_id: "trace-1",
      applied_at: "2026-01-01T10:00:00Z",
      receipt: { version: 0, historical_incomplete: true, columns: [COLUMNA_GUARDADA] },
    },
    last_attempt: null,
  } satisfies FileReceiptResponse);

  renderModal("f1");

  expect(await screen.findByText(/anterior al comprobante detallado/i)).toBeInTheDocument();
});
