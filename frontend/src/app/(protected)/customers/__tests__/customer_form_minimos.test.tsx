/**
 * Alta de cliente (decisión del dueño, 2026-11-10): nombre + UN contacto (celular o
 * email). Apellido y documento son opcionales. Los faltantes salen juntos y el aviso
 * se limpia al editar (antes sobrevivía al arreglo: el «error fantasma»).
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CustomersPage from "../page";
import { customersService } from "@/services/customers.service";

jest.mock("@/services/customers.service", () => ({
  customersService: {
    getAllCustomers: jest.fn().mockResolvedValue([]),
    createCustomer: jest.fn().mockResolvedValue({ id: "c-1" }),
    updateCustomer: jest.fn(),
    deleteCustomer: jest.fn(),
  },
}));
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) => selector({ add: jest.fn() }),
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
jest.mock("@/features/customers/CustomerFileModal", () => ({ CustomerFileModal: () => null }));

function abrirAlta() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CustomersPage />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getAllByRole("button", { name: /nuevo cliente/i })[0]!);
}

describe("alta de cliente: nombre + un contacto", () => {
  beforeEach(() => jest.clearAllMocks());

  it("sin contacto avisa y no guarda; al escribir el celular el aviso desaparece", async () => {
    abrirAlta();
    fireEvent.change(await screen.findByPlaceholderText("Juan"), { target: { value: "Caro" } });
    fireEvent.click(screen.getByRole("button", { name: /^guardar$/i }));
    expect(await screen.findByText(/Cargá al menos un contacto/)).toBeInTheDocument();
    expect(customersService.createCustomer).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText("+54 9 11 1234-5678"), {
      target: { value: "351555" },
    });
    expect(screen.queryByText(/Cargá al menos un contacto/)).toBeNull();
  });

  it("nombre + celular alcanza, sin apellido ni documento", async () => {
    abrirAlta();
    fireEvent.change(await screen.findByPlaceholderText("Juan"), { target: { value: "Caro" } });
    fireEvent.change(screen.getByPlaceholderText("+54 9 11 1234-5678"), {
      target: { value: "351555" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^guardar$/i }));
    await waitFor(() =>
      expect(customersService.createCustomer).toHaveBeenCalledWith(
        expect.objectContaining({ name: "Caro", phone: "351555" }),
      ),
    );
  });
});
