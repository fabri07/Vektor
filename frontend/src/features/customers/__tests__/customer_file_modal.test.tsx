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

describe("CustomerFileModal — F-N: propuesta de split nombre/apellido", () => {
  const mockConfirm = customersService.importConfirm as jest.Mock;

  const PREVIEW_CON_PROPUESTA: CustomerImportPreviewResponse = {
    items: [
      {
        row_index: 0,
        status: "create",
        customer: { name: "Juan Perez", dni: "30111222", customer_type: "person" },
        existing_id: null,
        existing_name: null,
        issues: [],
        name_split_suggestion: {
          status: "proposed",
          first_name: "Juan",
          last_name: "Perez",
          reason: "Sin coma: se propone la primera palabra como nombre.",
          confidence_basis: "customer_type=person",
        },
      },
      {
        row_index: 1,
        status: "create",
        customer: { name: "Roberto Gomez Sin Tipo", email: "roberto@x.com" },
        existing_id: null,
        existing_name: null,
        issues: [],
        name_split_suggestion: {
          status: "ambiguous",
          first_name: null,
          last_name: null,
          reason: "No hay evidencia suficiente de si es una persona o una empresa.",
          confidence_basis: "sin customer_type ni doc_type=dni",
        },
      },
      {
        row_index: 2,
        status: "create",
        customer: { name: "García e Hijos S.A.", cuit: "20-12345678-6", customer_type: "company" },
        existing_id: null,
        existing_name: null,
        issues: [],
        name_split_suggestion: {
          status: "not_applicable",
          first_name: null,
          last_name: null,
          reason: "Es una razón social (empresa) — el nombre queda entero.",
          confidence_basis: "customer_type=company",
        },
      },
    ],
    to_create: 3,
    to_update: 0,
    needs_review: 0,
    invalid: 0,
    duplicates: 0,
    warnings: [],
    source_upload_id: null,
  };

  beforeEach(() => {
    mockPreview.mockReset();
    mockConfirm.mockReset();
    mockPreview.mockResolvedValue(PREVIEW_CON_PROPUESTA);
    mockConfirm.mockResolvedValue({ created: 0, updated: 0, skipped: 0 });
  });

  it("muestra la propuesta con el botón Aplicar para la fila resuelta", async () => {
    renderModal();
    await uploadBulkFile();

    expect(await screen.findByText(/Nombre:/)).toBeInTheDocument();
    expect(screen.getByText("Juan", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Perez", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aplicar" })).toBeInTheDocument();
  });

  it("la fila ambigua explica pero no ofrece un botón de aplicar", async () => {
    renderModal();
    await uploadBulkFile();

    expect(
      await screen.findByText(/No está claro — revisalo si hace falta/),
    ).toBeInTheDocument();
  });

  it("la fila not_applicable (razón social) no muestra ningún hint", async () => {
    renderModal();
    await uploadBulkFile();

    await screen.findByText("García e Hijos S.A.");
    // Sólo debe existir el botón "Aplicar" de la fila proposed (1 en total).
    expect(screen.getAllByRole("button", { name: "Aplicar" })).toHaveLength(1);
  });

  it("«Aplicar» separa el nombre en la fila y desaparece el hint", async () => {
    renderModal();
    await uploadBulkFile();

    fireEvent.click(await screen.findByRole("button", { name: "Aplicar" }));

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Aplicar" })).not.toBeInTheDocument();
    });
  });

  it("confirmar después de aplicar manda el nombre y apellido ya separados", async () => {
    renderModal();
    await uploadBulkFile();

    fireEvent.click(await screen.findByRole("button", { name: "Aplicar" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Aplicar" })).not.toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /Importar 3 clientes/ }));

    await waitFor(() => expect(mockConfirm).toHaveBeenCalled());
    const rows = mockConfirm.mock.calls[0]?.[0];
    const fila0 = rows.find((r: { dni?: string }) => r.dni === "30111222");
    expect(fila0.name).toBe("Juan");
    expect(fila0.last_name).toBe("Perez");
  });
});
