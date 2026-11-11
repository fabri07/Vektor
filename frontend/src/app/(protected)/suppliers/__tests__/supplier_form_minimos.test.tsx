/**
 * Alta de proveedor (decisión del dueño, 2026-11-10): nombre + UN contacto
 * (teléfono o email). CUIT/CUIL y forma de pago son opcionales, y los faltantes
 * salen todos juntos (antes: un error por intento, cuatro reintentos).
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import SuppliersPage from "../page";
import { suppliersService } from "@/services/suppliers.service";

jest.mock("@/services/suppliers.service", () => ({
  suppliersService: {
    getAllSuppliers: jest.fn().mockResolvedValue([]),
    createSupplier: jest.fn().mockResolvedValue({ id: "s-1" }),
    updateSupplier: jest.fn(),
    deleteSupplier: jest.fn(),
  },
}));
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) => selector({ add: jest.fn() }),
}));
jest.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: unknown }) => unknown) =>
    selector({ user: { role_code: "OWNER" } }),
}));
jest.mock("@/features/customFields/useBusinessFieldColumns", () => ({
  useBusinessFieldColumns: () => ({ columns: [], isLoading: false }),
}));
jest.mock("@/features/customFields/useSaveCustomField", () => ({
  useSaveCustomField: () => ({ mutate: jest.fn() }),
}));
jest.mock("@/features/customFields/AddColumnButton", () => ({ AddColumnButton: () => null }));
jest.mock("@/features/customFields/ExportAllFieldsButton", () => ({
  ExportAllFieldsButton: () => null,
}));
jest.mock("@/features/customFields/AllDataModal", () => ({ AllDataModal: () => null }));

function abrirAlta() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <SuppliersPage />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getAllByRole("button", { name: /nuevo proveedor/i })[0]!);
}

describe("alta de proveedor: nombre + un contacto", () => {
  beforeEach(() => jest.clearAllMocks());

  it("sin contacto avisa y no guarda; al escribir el teléfono el aviso desaparece", async () => {
    abrirAlta();
    fireEvent.change(await screen.findByPlaceholderText("Ej: Distribuidora del Sur"), {
      target: { value: "Mayorista La Salada" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^guardar$/i }));
    expect(await screen.findByText(/Cargá al menos un contacto/)).toBeInTheDocument();
    expect(suppliersService.createSupplier).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText("+54 9 11 1234-5678"), {
      target: { value: "351555" },
    });
    expect(screen.queryByText(/Cargá al menos un contacto/)).toBeNull();
  });

  it("junta todos los faltantes en un solo aviso", async () => {
    abrirAlta();
    await screen.findByPlaceholderText("Ej: Distribuidora del Sur");
    fireEvent.change(screen.getByPlaceholderText("30-71234567-8"), {
      target: { value: "30-11111111-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^guardar$/i }));
    const aviso = await screen.findByText(/nombre o razón social es requerido/i);
    expect(aviso).toHaveTextContent(/Cargá al menos un contacto/);
    expect(aviso).toHaveTextContent(/CUIT inválido/);
  });

  it("nombre + teléfono alcanza, sin CUIT ni forma de pago", async () => {
    abrirAlta();
    fireEvent.change(await screen.findByPlaceholderText("Ej: Distribuidora del Sur"), {
      target: { value: "Mayorista La Salada" },
    });
    fireEvent.change(screen.getByPlaceholderText("+54 9 11 1234-5678"), {
      target: { value: "351555" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^guardar$/i }));
    await waitFor(() =>
      expect(suppliersService.createSupplier).toHaveBeenCalledWith(
        expect.objectContaining({ name: "Mayorista La Salada", phone: "351555", cuit: null }),
      ),
    );
  });
});
