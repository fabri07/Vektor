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

// `?file=<id>` abre ese archivo expandido (el usuario llega así desde el
// cierre del onboarding). Sin parámetro, la lista arranca toda colapsada.
const mockSearchParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

// El panel de mapeo hace sus propias llamadas (preview, catálogo de campos,
// sugerencias); acá sólo interesa SI está montado, no qué muestra adentro.
jest.mock("../ColumnMapperPanel", () => ({
  ColumnMapperPanel: () => <div data-testid="column-mapper-panel" />,
}));

const mockAddToast = jest.fn();
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: mockAddToast }),
}));

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    listFiles: jest.fn(),
    getFile: jest.fn(),
    deleteFile: jest.fn(),
    reprocessFile: jest.fn(),
    rereadPreview: jest.fn(),
    rereadApply: jest.fn(),
    rereadRunStatus: jest.fn(),
    rereadUndo: jest.fn(),
  },
}));

const mockListFiles = ingestionService.listFiles as jest.Mock;
const mockGetFile = ingestionService.getFile as jest.Mock;
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

function setupRereadMocks() {
  mockListFiles.mockResolvedValue([fileWith("DONE")]);
  mockRereadPreview.mockResolvedValue(PREVIEW_RESPONSE);
  mockRereadApply.mockResolvedValue(APPLY_START_RESPONSE);
  mockRereadRunStatus.mockResolvedValue(appliedRunStatus());
}

/**
 * Recorre el modal de relectura hasta la fase "result" (botón "Deshacer
 * relectura" visible): Volver a leer → confirmar arranque → preview →
 * aplicar → polling hasta APPLIED. Mismo camino que recorre un usuario real;
 * no hay atajo para inyectar `rereadResults` desde afuera del componente.
 * No renderiza — se puede llamar más de una vez sobre el mismo componente ya
 * montado, para simular una SEGUNDA relectura+undo del mismo archivo.
 */
async function rereadFlowToResultPhase(user: ReturnType<typeof userEvent.setup>) {
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

/** Setea los mocks, renderiza y recorre el flujo una vez. */
async function driveToResultPhase(user: ReturnType<typeof userEvent.setup>) {
  setupRereadMocks();
  renderList();
  await rereadFlowToResultPhase(user);
}

/** Click en "Deshacer relectura" → confirmar → esperar a que el backend responda. */
async function confirmUndo(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /^deshacer relectura$/i }));
  await waitFor(() =>
    expect(
      screen.getByRole("heading", { name: "Deshacer relectura" }),
    ).toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: /sí, deshacer/i }));
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

  // Hallazgo Important del revisor: un undo posterior que sí se revierte
  // limpio debe limpiar el aviso de un undo anterior con entidades no
  // revertidas — si no, el banner queda pegado en pantalla mal atribuido a
  // la acción recién hecha.
  test("un undo posterior limpio limpia el aviso de un undo anterior con entidades no revertidas", async () => {
    const user = userEvent.setup();
    mockRereadUndo
      .mockResolvedValueOnce({
        run_id: "run-1",
        restored: 1,
        removed: 1,
        status: "REVERTED",
        not_reverted_entities: [
          { kind: "customer", id: "cust-1", reason: "edited_after_reread" },
        ],
      } satisfies RereadUndoResponse)
      .mockResolvedValueOnce({
        run_id: "run-2",
        restored: 2,
        removed: 0,
        status: "REVERTED",
        not_reverted_entities: [],
      } satisfies RereadUndoResponse);

    // Primera relectura + undo: queda not_reverted_entities → aparece el aviso.
    await driveToResultPhase(user);
    await confirmUndo(user);
    await waitFor(() => expect(mockRereadUndo).toHaveBeenCalledTimes(1));
    const staleNotice = await screen.findByRole("status");
    expect(
      within(staleNotice).getByText(/no se revirtió el cliente/i),
    ).toBeInTheDocument();

    // Segunda relectura + undo del MISMO archivo, ya limpia (sin entidades
    // no revertidas) — el aviso viejo no debe seguir en pantalla.
    await rereadFlowToResultPhase(user);
    await confirmUndo(user);
    await waitFor(() => expect(mockRereadUndo).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
  });
});

/**
 * La lista caía a `[]` ante cualquier fallo de la API y la pantalla afirmaba
 * "No hay archivos cargados todavía" — con archivos en la base. Un error de
 * red no es un estado vacío: la UI no puede afirmar que no hay nada cuando lo
 * que pasó es que no pudo preguntar.
 */
describe("FileListSection — la API falla", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("un error no se renderiza como 'no hay archivos'", async () => {
    mockListFiles.mockRejectedValue(new Error("Network Error"));

    renderList();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /no pudimos cargar tus archivos/i,
      ),
    );
    expect(
      screen.queryByText(/no hay archivos cargados todavía/i),
    ).not.toBeInTheDocument();
  });

  test("se puede reintentar sin recargar la página", async () => {
    const user = userEvent.setup();
    mockListFiles.mockRejectedValueOnce(new Error("Network Error"));

    renderList();
    await screen.findByRole("alert");

    mockListFiles.mockResolvedValue([fileWith("NEEDS_CONFIRMATION")]);
    await user.click(screen.getByRole("button", { name: /reintentar/i }));

    expect(await screen.findByText("ventas.csv")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

/**
 * El cierre del onboarding manda a `/ingestion?file=<id>` diciéndole al
 * usuario que le falta revisar ESE archivo. Si aterriza en una tabla toda
 * colapsada, lo mandamos a buscar cuál era.
 */
describe("FileListSection — llegada desde el onboarding", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSearchParams.delete("file");
  });

  test("?file=<id> abre ese archivo ya expandido", async () => {
    mockSearchParams.set("file", "file-1");
    mockListFiles.mockResolvedValue([fileWith("NEEDS_CONFIRMATION")]);

    renderList();

    // El panel de mapeo del archivo apuntado ya está en pantalla, sin clicks.
    expect(await screen.findByTestId("column-mapper-panel")).toBeInTheDocument();
  });

  test("sin ?file la lista arranca colapsada", async () => {
    mockListFiles.mockResolvedValue([fileWith("NEEDS_CONFIRMATION")]);

    renderList();

    await screen.findByText("ventas.csv");
    expect(screen.queryByTestId("column-mapper-panel")).not.toBeInTheDocument();
  });
});

/**
 * Dos huecos del deep link, encontrados en review:
 *  - `expandedId` se sembraba con un inicializador de `useState`, que sólo
 *    corre al montar: navegar client-side a `?file=` estando ya en /ingestion
 *    no abría nada, en silencio.
 *  - El panel sólo se monta con `NEEDS_CONFIRMATION`. El archivo recién subido
 *    se parsea async, así que puede aterrizar en PROCESSING, FAILED o
 *    NEEDS_COMPLETION — y el usuario venía de un botón que le prometió
 *    "Revisar mi archivo".
 */
describe("FileListSection — deep link que no puede abrir el panel", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSearchParams.delete("file");
  });

  test("archivo apuntado todavía procesando: lo dice en vez de no hacer nada", async () => {
    mockSearchParams.set("file", "file-1");
    mockListFiles.mockResolvedValue([fileWith("PROCESSING")]);

    renderList();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /estamos leyendo .*ventas\.csv/i,
    );
    expect(screen.queryByTestId("column-mapper-panel")).not.toBeInTheDocument();
  });

  test("archivo apuntado que falló: lo dice", async () => {
    mockSearchParams.set("file", "file-1");
    mockListFiles.mockResolvedValue([fileWith("FAILED")]);

    renderList();

    expect(await screen.findByRole("status")).toHaveTextContent(/no pudimos leer/i);
  });

  test("archivo ya importado no genera aviso", async () => {
    mockSearchParams.set("file", "file-1");
    mockListFiles.mockResolvedValue([fileWith("DONE")]);

    renderList();

    await screen.findByText("ventas.csv");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("FileListSection — archivo apuntado fuera de la página del listado", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSearchParams.delete("file");
  });

  test("un archivo viejo que existe NO se reporta como eliminado", async () => {
    // El listado devuelve de a 50 ordenados por fecha: un archivo viejo puede
    // no estar ahí y seguir existiendo. Antes eso se leía como "se eliminó".
    mockSearchParams.set("file", "file-viejo");
    mockListFiles.mockResolvedValue([fileWith("DONE")]);
    mockGetFile.mockResolvedValue({
      ...fileWith("PROCESSING"),
      id: "file-viejo",
      original_filename: "marzo.xlsx",
    });

    renderList();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /estamos leyendo .*marzo\.xlsx/i,
    );
    expect(mockGetFile).toHaveBeenCalledWith("file-viejo");
  });

  test("recién con un 404 se afirma que se eliminó", async () => {
    mockSearchParams.set("file", "file-fantasma");
    mockListFiles.mockResolvedValue([fileWith("DONE")]);
    mockGetFile.mockResolvedValue(null); // 404 del backend

    renderList();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /puede que se haya eliminado/i,
    );
  });

  test("si la consulta falla no se afirma nada", async () => {
    // No poder preguntar no es evidencia de que el archivo no exista.
    mockSearchParams.set("file", "file-viejo");
    mockListFiles.mockResolvedValue([fileWith("DONE")]);
    mockGetFile.mockRejectedValue(new Error("500"));

    renderList();

    await screen.findByText("ventas.csv");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
