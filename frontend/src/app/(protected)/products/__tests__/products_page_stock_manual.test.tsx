import "@testing-library/jest-dom";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders } from "axios";

import ProductsPage from "../page";
import { productsService } from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { fieldCatalogService } from "@/services/fieldCatalog.service";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

/**
 * M·P2c: editar el stock a mano. Una suba pregunta si es una compra (va por
 * «Registrar compra») o una corrección de conteo (ajuste manual con precio de
 * compra); el PATCH viaja con el stock que se veía, para que el servidor no
 * pise una venta del medio.
 */

jest.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
jest.mock("@/services/products.service", () => {
  const actual = jest.requireActual("@/services/products.service");
  return {
    ...actual,
    productsService: {
      getAllProducts: jest.fn(),
      countProducts: jest.fn(),
      getCategories: jest.fn(),
      createCategory: jest.fn(),
      updateProduct: jest.fn(),
      deleteProduct: jest.fn(),
    },
  };
});
jest.mock("@/services/fieldDefinitions.service", () => ({
  fieldDefinitionsService: { getAll: jest.fn() },
}));
jest.mock("@/services/fieldCatalog.service", () => ({
  fieldCatalogService: { getAvailableFields: jest.fn() },
}));
jest.mock("@/services/entityExport.service", () => ({
  entityExportService: { exportAll: jest.fn() },
}));

const mockGetAllProducts = productsService.getAllProducts as jest.Mock;
const mockCountProducts = productsService.countProducts as jest.Mock;
const mockGetCategories = productsService.getCategories as jest.Mock;
const mockCreateCategory = productsService.createCategory as jest.Mock;
const mockUpdateProduct = productsService.updateProduct as jest.Mock;
const mockGetFieldDefs = fieldDefinitionsService.getAll as jest.Mock;
const mockGetAvailable = fieldCatalogService.getAvailableFields as jest.Mock;

const PRODUCT = {
  id: "p1",
  tenant_id: "t1",
  name: "Lámpara de pie",
  sku: null,
  internal_sku: "VKT-001",
  description: null,
  category: null,
  sale_price_ars: 15000,
  unit_cost_ars: 9000,
  stock_units: 3,
  low_stock_threshold_units: null,
  is_active: true,
  margin_pct: 40,
  is_low_stock: false,
  stock_status: "in_stock" as const,
  custom_fields: {},
  category_suggestion: { code: "ILUMINACION", label: "Iluminación" },
};

const CATALOGO = [
  { code: "ILUMINACION", label: "Iluminación" },
  { code: "OTHER", label: "Otros" },
];

function comoRol(role: string) {
  useAuthStore.setState({
    user: { id: "u1", email: "a@a.com", full_name: "A", role, tenant_id: "t1" },
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetAllProducts.mockResolvedValue([PRODUCT]);
  mockCountProducts.mockResolvedValue(1);
  mockGetCategories.mockResolvedValue(CATALOGO);
  mockUpdateProduct.mockResolvedValue(PRODUCT);
  mockGetFieldDefs.mockResolvedValue([]);
  mockGetAvailable.mockResolvedValue([]);
  comoRol("OWNER");
});

function renderizar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidar = jest.spyOn(qc, "invalidateQueries");
  render(
    <QueryClientProvider client={qc}>
      <ProductsPage />
    </QueryClientProvider>,
  );
  return { invalidar };
}

async function abrirEditor() {
  await waitFor(() => expect(screen.getByText("Sugerida: Iluminación")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /elegir otra categoría/i }));
  return screen.findByRole("dialog");
}

function stock(dialogo: HTMLElement, valor: string) {
  fireEvent.change(within(dialogo).getByLabelText(/^Stock/), { target: { value: valor } });
}

test("una suba pide el motivo; como corrección viaja con el stock esperado y el envío", async () => {
  renderizar();
  const dialogo = await abrirEditor();
  stock(dialogo, "5");

  const guardar = within(dialogo).getByRole("button", { name: "Guardar" });
  expect(within(dialogo).getByText("¿Por qué sube el stock?")).toBeInTheDocument();
  expect(guardar).toBeDisabled();

  fireEvent.click(within(dialogo).getByLabelText(/Corrección de conteo/));
  fireEvent.change(within(dialogo).getByLabelText(/incluye el envío/), {
    target: { value: "incluido" },
  });
  expect(guardar).toBeEnabled();
  fireEvent.click(guardar);

  await waitFor(() =>
    expect(mockUpdateProduct).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({
        stock_units: 5,
        stock_units_esperado: 3,
        costo_envio: "incluido",
      }),
    ),
  );
});

test("si la compró ahora, no deja guardar la suba y manda a Registrar compra", async () => {
  renderizar();
  const dialogo = await abrirEditor();
  stock(dialogo, "5");
  fireEvent.click(within(dialogo).getByLabelText(/La compré ahora/));

  expect(within(dialogo).getByRole("alert")).toHaveTextContent(/Registrar compra/);
  expect(within(dialogo).getByRole("button", { name: "Guardar" })).toBeDisabled();
});

test("una baja no pregunta y viaja con el stock esperado, sin envío", async () => {
  renderizar();
  const dialogo = await abrirEditor();
  stock(dialogo, "1");
  expect(within(dialogo).queryByText("¿Por qué sube el stock?")).not.toBeInTheDocument();

  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(mockUpdateProduct).toHaveBeenCalled());
  const payload = mockUpdateProduct.mock.calls[0][1];
  expect(payload).toMatchObject({ stock_units: 1, stock_units_esperado: 3 });
  expect(payload).not.toHaveProperty("costo_envio");
});

test("sin tocar el stock, el PATCH no lleva stock", async () => {
  renderizar();
  const dialogo = await abrirEditor();
  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(mockUpdateProduct).toHaveBeenCalled());
  const payload = mockUpdateProduct.mock.calls[0][1];
  expect(payload).not.toHaveProperty("stock_units");
  expect(payload).not.toHaveProperty("stock_units_esperado");
});

test("una corrección en un producto sin costo pide el precio de compra", async () => {
  mockGetAllProducts.mockResolvedValue([{ ...PRODUCT, unit_cost_ars: null }]);
  renderizar();
  const dialogo = await abrirEditor();
  stock(dialogo, "5");
  fireEvent.click(within(dialogo).getByLabelText(/Corrección de conteo/));

  expect(within(dialogo).getByRole("alert")).toHaveTextContent(/precio de compra/);
  expect(within(dialogo).getByRole("button", { name: "Guardar" })).toBeDisabled();
});

test("un 409 STOCK_CAMBIO avisa que entró una venta", async () => {
  mockUpdateProduct.mockRejectedValue(
    new AxiosError("conflict", "409", undefined, undefined, {
      status: 409,
      statusText: "Conflict",
      headers: {},
      config: { headers: new AxiosHeaders() },
      data: { detail: { code: "STOCK_CAMBIO", message: "cambió" } },
    }),
  );
  const { invalidar } = renderizar();
  const dialogo = await abrirEditor();
  stock(dialogo, "1");
  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));
  // Refresca la lista: reabrir con el stock viejo repetiría el 409.
  await waitFor(() =>
    expect(invalidar).toHaveBeenCalledWith({ queryKey: ["products-list"] }),
  );
  await waitFor(() =>
    expect(useToastStore.getState().toasts.map((t) => t.message)).toEqual(
      expect.arrayContaining([expect.stringMatching(/El stock cambió mientras editabas/)]),
    ),
  );
});
