import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { FileListSection } from "../FileListSection";
import {
  ingestionService,
  type UploadedFileItem,
  type RereadApplyResponse,
  type RereadUndoResponse,
} from "@/services/ingestion.service";

const mockAddToast = jest.fn();
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: mockAddToast }),
}));

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    listFiles: jest.fn(),
    deleteFile: jest.fn(),
    reprocessFile: jest.fn(),
    rereadPreview: jest.fn(),
    rereadApply: jest.fn(),
    rereadRunStatus: jest.fn(),
    rereadUndo: jest.fn(),
  },
}));

const mockListFiles = ingestionService.listFiles as jest.Mock;
const mockRereadPreview = ingestionService.rereadPreview as jest.Mock;
const mockRereadApply = ingestionService.rereadApply as jest.Mock;
const mockRereadRunStatus = ingestionService.rereadRunStatus as jest.Mock;
const mockRereadUndo = ingestionService.rereadUndo as jest.Mock;

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

// ── F9b: confirmación antes de deshacer + aviso de entidades no revertidas ──

const PREVIEW_RESPONSE = {
  file_id: "file-1",
  counts: {
    to_update: 1,
    preserved: 2,
    new: 0,
    to_void: 0,
    unchanged: 0,
    products_new: 0,
    products_restock: 0,
  },
  legacy_fallback: false,
  sample_changes: [],
};

const APPLY_START_RESPONSE = { file_id: "file-1", run_id: "run-1", status: "RUNNING" };

function appliedRunStatus(): RereadApplyResponse & {
  run_id: string;
  file_id: string;
  status: string;
  error: string | null;
} {
  return {
    run_id: "run-1",
    file_id: "file-1",
    status: "APPLIED",
    to_update: 1,
    preserved: 2,
    new: 0,
    voided: 0,
    inserted: 1,
    legacy_fallback: false,
    items: [],
    error: null,
  };
}

/**
 * Lleva el modal de relectura hasta la fase "result" (botón "Deshacer
 * relectura" visible): Volver a leer → confirmar arranque → preview →
 * aplicar → polling hasta APPLIED. Mismo camino que recorre un usuario real;
 * no hay atajo para inyectar `rereadResults` desde afuera del componente.
 */
async function driveToResultPhase(user: ReturnType<typeof userEvent.setup>) {
  mockListFiles.mockResolvedValue([fileWith("DONE")]);
  mockRereadPreview.mockResolvedValue(PREVIEW_RESPONSE);
  mockRereadApply.mockResolvedValue(APPLY_START_RESPONSE);
  mockRereadRunStatus.mockResolvedValue(appliedRunStatus());

  renderList();

  await waitFor(() =>
    expect(screen.getByTitle("Volver a leer este archivo")).toBeInTheDocument(),
  );
  await user.click(screen.getByTitle("Volver a leer este archivo"));

  await waitFor(() =>
    expect(screen.getByRole("button", { name: /sí, releer/i })).toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: /sí, releer/i }));

  await waitFor(() =>
    expect(screen.getByRole("button", { name: /aplicar relectura/i })).toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: /aplicar relectura/i }));

  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /^deshacer relectura$/i }),
    ).toBeInTheDocument(),
  );
}

describe("FileListSection — deshacer relectura (F9b)", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("click en 'Deshacer relectura' sin confirmar no llama al backend", async () => {
    const user = userEvent.setup();
    await driveToResultPhase(user);

    await user.click(screen.getByRole("button", { name: /^deshacer relectura$/i }));

    // Se abre el modal de confirmación con su copy — el backend todavía no
    // fue llamado.
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Deshacer relectura" }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/solo la última relectura/i)).toBeInTheDocument();
    expect(
      screen.getByText(/se revierten las ventas, gastos y stock/i),
    ).toBeInTheDocument();
    expect(mockRereadUndo).not.toHaveBeenCalled();

    // Cancelar tampoco lo dispara.
    await user.click(screen.getByRole("button", { name: /^cancelar$/i }));
    expect(mockRereadUndo).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Deshacer relectura" }),
      ).not.toBeInTheDocument(),
    );
  });

  test("confirmar 'Sí, deshacer' sí llama al backend con el fileId", async () => {
    const user = userEvent.setup();
    mockRereadUndo.mockResolvedValue({
      run_id: "run-1",
      restored: 2,
      removed: 1,
      status: "REVERTED",
      not_reverted_entities: [],
    } satisfies RereadUndoResponse);

    await driveToResultPhase(user);

    await user.click(screen.getByRole("button", { name: /^deshacer relectura$/i }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Deshacer relectura" }),
      ).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /sí, deshacer/i }));

    await waitFor(() => expect(mockRereadUndo).toHaveBeenCalledWith("file-1"));
    // Sin entidades no revertidas: no aparece el aviso persistente.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  test("respuesta con not_reverted_entities no vacío renderiza el aviso", async () => {
    const user = userEvent.setup();
    mockRereadUndo.mockResolvedValue({
      run_id: "run-1",
      restored: 1,
      removed: 1,
      status: "REVERTED",
      not_reverted_entities: [
        { kind: "customer", id: "cust-1", reason: "edited_after_reread" },
        { kind: "product", id: "prod-1", reason: "edited_after_reread" },
      ],
    } satisfies RereadUndoResponse);

    await driveToResultPhase(user);

    await user.click(screen.getByRole("button", { name: /^deshacer relectura$/i }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Deshacer relectura" }),
      ).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /sí, deshacer/i }));

    await waitFor(() => expect(mockRereadUndo).toHaveBeenCalledWith("file-1"));

    const notice = await screen.findByRole("status");
    expect(within(notice).getByText(/no se revirtió el cliente/i)).toBeInTheDocument();
    expect(within(notice).getByText(/no se revirtió el producto/i)).toBeInTheDocument();

    // Cliente/proveedor tienen ficha navegable → link; producto no.
    const links = within(notice).getAllByRole("link", { name: /ver ficha/i });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/customers/cust-1");
  });
});
