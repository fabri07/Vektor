import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { FileUploadSection } from "../FileUploadSection";
import { ingestionService } from "@/services/ingestion.service";

/**
 * El hueco por el que entró el bug.
 *
 * `FileUploadSection` tenía un confirm PROPIO —la tercera implementación del
 * confirm en el frontend— que llamaba a `confirmFile` con `column_mappings: []`.
 * Sin mapeos el backend cae a la heurística de encabezados, así que
 * "Especificaciones" (→ descripción) y "Tienda" (→ proveedor) no llegaban nunca
 * al payload por este camino, que además es el que el usuario toma por defecto:
 * el panel aparece arriba, apenas termina de subir, con un botón listo para
 * apretar.
 *
 * No existía NINGÚN test de este componente. Los del panel de mapeo mockean
 * `ingestionService` entero y verifican el payload; ninguno miraba quién más
 * podía confirmar por su cuenta.
 */

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    upload: jest.fn(),
    getPreview: jest.fn(),
    confirmFile: jest.fn(),
    reprocessFile: jest.fn(),
    getColumnMappings: jest.fn(),
    getFieldCatalog: jest.fn(),
    cancelFile: jest.fn(),
    recomputeColumnRisk: jest.fn(),
    fetchInventoryEffects: jest.fn(),
    fetchPurchaseGroups: jest.fn(),
  },
}));

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: jest.fn() }),
}));

const mockUpload = ingestionService.upload as jest.Mock;
const mockGetPreview = ingestionService.getPreview as jest.Mock;
const mockConfirmFile = ingestionService.confirmFile as jest.Mock;
const mockGetColumnMappings = ingestionService.getColumnMappings as jest.Mock;
const mockGetFieldCatalog = ingestionService.getFieldCatalog as jest.Mock;

const FILE_ID = "11111111-1111-4111-8111-111111111111";

const PREVIEW = {
  file_id: FILE_ID,
  processing_status: "NEEDS_CONFIRMATION",
  parsed_summary_json: {
    inferred_type: "stock",
    mapping_contexts: [
      {
        context_id: "sheet:catalogo",
        label: "Catálogo",
        source_kind: "table",
        entity_type: "product",
        headers: ["Nombre", "Especificaciones", "Tienda"],
        preview_rows: [],
        row_count: 3,
      },
    ],
  },
  master_previews: [],
};

const CATALOGO = {
  product: {
    required: ["name"],
    required_alternatives: {},
    fields: [
      { value: "name", label: "Nombre", single_value: true },
      { value: "description", label: "Descripción", single_value: false },
    ],
    cross_fields: [{ value: "supplier:name", label: "Proveedor", single_value: true }],
  },
};

function renderizar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <FileUploadSection />
      </QueryClientProvider>,
    ),
  };
}

async function subirArchivo(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const archivo = new File(["x"], "catalogo.xlsx", {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  fireEvent.change(input, { target: { files: [archivo] } });
  fireEvent.click(await screen.findByRole("button", { name: /subir archivo/i }));
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
  mockUpload.mockResolvedValue({ file_id: FILE_ID });
  mockGetPreview.mockResolvedValue(PREVIEW);
  mockGetColumnMappings.mockResolvedValue([]);
  mockGetFieldCatalog.mockResolvedValue(CATALOGO);
});

afterEach(() => {
  jest.useRealTimers();
});

async function llegarANeedsConfirmation(container: HTMLElement) {
  await subirArchivo(container);
  await waitFor(() => expect(mockUpload).toHaveBeenCalled());
  await jest.advanceTimersByTimeAsync(2_500);
  await waitFor(() => expect(mockGetPreview).toHaveBeenCalledWith(FILE_ID));
}

test("tras el parseo abre el panel de mapeo, no un confirm propio", async () => {
  const { container } = renderizar();
  await llegarANeedsConfirmation(container);

  // El panel de mapeo se monta: pide el catálogo de campos y los mapeos sugeridos.
  // Ninguno de los dos lo pedía el camino viejo.
  await waitFor(() => expect(mockGetFieldCatalog).toHaveBeenCalled());
  expect(mockGetColumnMappings).toHaveBeenCalled();

  // Y el botón del camino viejo ya no existe.
  expect(
    screen.queryByRole("button", { name: /confirmar datos/i }),
  ).not.toBeInTheDocument();
});

test("FileUploadSection no confirma por su cuenta", async () => {
  const { container } = renderizar();
  await llegarANeedsConfirmation(container);
  await waitFor(() => expect(mockGetFieldCatalog).toHaveBeenCalled());

  // Nadie confirmó nada sin pasar por el mapeo: es exactamente la regresión que
  // dejó `description: 0` y un solo proveedor centinela en la cuenta real.
  expect(mockConfirmFile).not.toHaveBeenCalled();
});

test("el preview del polling se siembra en la cache que lee el panel", async () => {
  const { container, qc } = renderizar();
  await llegarANeedsConfirmation(container);

  // El polling ya tenía el preview en la mano y lo deja bajo la MISMA queryKey que
  // usa el panel, así que éste renderiza con datos desde el primer frame en vez de
  // arrancar en blanco esperando un GET que ya se hizo.
  expect(qc.getQueryData(["ingestion-preview", FILE_ID])).toEqual(PREVIEW);
});
