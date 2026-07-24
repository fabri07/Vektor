import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CustomerFileModal } from "../CustomerFileModal";
import {
  customersService,
  type CustomerImportPreviewResponse,
} from "@/services/customers.service";

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: jest.fn() }),
}));

jest.mock("@/services/customers.service", () => ({
  customersService: {
    extractCustomer: jest.fn(),
    importPreview: jest.fn(),
    importConfirm: jest.fn(),
  },
}));

const mockPreview = customersService.importPreview as jest.Mock;

// Preview con una fila needs_review (dato válido pero sin clave de identidad
// fuerte → no se crea, se marca para revisión) + una inválida, para contrastar.
const PREVIEW: CustomerImportPreviewResponse = {
  items: [
    {
      row_index: 0,
      status: "needs_review",
      customer: { name: "Ana Sin Documento" },
      existing_id: null,
      existing_name: null,
      issues: ["Sin documento ni email para identificar"],
    },
    {
      row_index: 1,
      status: "invalid",
      customer: { name: "Cuit Roto", cuit: "99-99999999-9" },
      existing_id: null,
      existing_name: null,
      issues: ["CUIT inválido"],
    },
    {
      row_index: 2,
      status: "create",
      customer: { name: "Nuevo OK", cuit: "20-12345678-6" },
      existing_id: null,
      existing_name: null,
      issues: [],
    },
  ],
  to_create: 1,
  to_update: 0,
  needs_review: 1,
  invalid: 1,
  duplicates: 0,
  warnings: [],
  source_upload_id: null,
};

function renderModal() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CustomerFileModal
        isOpen
        onClose={jest.fn()}
        onExtracted={jest.fn()}
        onImported={jest.fn()}
      />
    </QueryClientProvider>,
  );
}

async function uploadBulkFile() {
  // pasar a modo "Lista de clientes" y subir una planilla (Modal usa portal → document)
  fireEvent.click(screen.getByText("Lista de clientes"));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["nombre\nAna"], "clientes.csv", { type: "text/csv" });
  fireEvent.change(input, { target: { files: [file] } });
  await waitFor(() => expect(mockPreview).toHaveBeenCalled());
}

describe("CustomerFileModal — estado needs_review", () => {
  beforeEach(() => {
    mockPreview.mockReset();
    mockPreview.mockResolvedValue(PREVIEW);
  });

  it("muestra el card 'A revisar' con el conteo de needs_review", async () => {
    renderModal();
    await uploadBulkFile();

    const card = await screen.findByText("A revisar");
    expect(card).toBeInTheDocument();
    // el valor del card (1) vive en el mismo bloque que el label
    expect(card.parentElement).toHaveTextContent("1");
  });

  it("etiqueta la fila needs_review como 'Revisar', NO como 'Inválido'", async () => {
    renderModal();
    await uploadBulkFile();

    // la fila needs_review se muestra como "Revisar: <motivo>"
    expect(await screen.findByText(/^Revisar/)).toBeInTheDocument();
    // y NO se la confunde con la inválida: solo la fila realmente inválida dice
    // "Inválido" (lookahead evita matchear el card de resumen "Inválidos").
    const invalidos = screen.getAllByText(/^Inválido(?!s)/);
    expect(invalidos).toHaveLength(1);
  });
});
