/**
 * B·5d: en la edición del producto, una suba de stock de un producto sin stock
 * inicial se puede declarar «Es mi stock inicial»: el PATCH va sin stock y
 * después `POST /products/{id}/opening` con la diferencia, el costo y la fecha.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders } from "axios";

import ProductsPage from "@/app/(protected)/products/page";
import { productsService } from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { fieldCatalogService } from "@/services/fieldCatalog.service";
import { useAuthStore } from "@/stores/authStore";

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
      registerOpening: jest.fn(),
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
const mockUpdateProduct = productsService.updateProduct as jest.Mock;
const mockRegisterOpening = productsService.registerOpening as jest.Mock;

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
  needs_initial_stock: true,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockGetAllProducts.mockResolvedValue([PRODUCT]);
  (productsService.countProducts as jest.Mock).mockResolvedValue(1);
  (productsService.getCategories as jest.Mock).mockResolvedValue([]);
  mockUpdateProduct.mockResolvedValue(PRODUCT);
  mockRegisterOpening.mockResolvedValue({
    movement_id: "m1",
    product_id: "p1",
    stock_units: 7,
    ventas_sin_costo_desde_la_fecha: 0,
  });
  (fieldDefinitionsService.getAll as jest.Mock).mockResolvedValue([]);
  (fieldCatalogService.getAvailableFields as jest.Mock).mockResolvedValue([]);
  useAuthStore.setState({
    user: { id: "u1", email: "a@a.com", full_name: "A", role: "OWNER", tenant_id: "t1" },
  } as never);
});

async function abrirEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ProductsPage />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Editar producto" }));
  const dialogo = await screen.findByRole("dialog");
  fireEvent.change(within(dialogo).getByLabelText(/^Stock/), { target: { value: "10" } });
  return dialogo;
}

test("«Es mi stock inicial» manda el PATCH sin stock y la apertura con la diferencia", async () => {
  const dialogo = await abrirEditor();
  fireEvent.click(within(dialogo).getByLabelText(/Es mi stock inicial/));
  const guardar = within(dialogo).getByRole("button", { name: "Guardar" });
  // Sin fecha no se guarda.
  expect(guardar).toBeDisabled();
  fireEvent.change(within(dialogo).getByLabelText("Fecha del stock inicial"), {
    target: { value: "2026-08-01" },
  });
  fireEvent.change(within(dialogo).getByLabelText(/incluye el envío/), {
    target: { value: "no_aplica" },
  });
  expect(guardar).toBeEnabled();
  fireEvent.click(guardar);

  await waitFor(() => expect(mockRegisterOpening).toHaveBeenCalled());
  const patch = mockUpdateProduct.mock.calls[0][1];
  expect(patch).not.toHaveProperty("stock_units");
  expect(patch).not.toHaveProperty("stock_units_esperado");
  const [id, body, clave] = mockRegisterOpening.mock.calls[0];
  expect(id).toBe("p1");
  expect(body).toEqual({
    stock_units: 7,
    unit_cost_ars: 9000,
    fecha: "2026-08-01T00:00:00",
    costo_envio: "no_aplica",
  });
  expect(typeof clave).toBe("string");
  // El PATCH va antes que la apertura.
  expect(mockUpdateProduct.mock.invocationCallOrder[0]!).toBeLessThan(
    mockRegisterOpening.mock.invocationCallOrder[0]!,
  );
});

test("OPENING_EXISTS muestra los caminos que da el servidor", async () => {
  mockRegisterOpening.mockRejectedValue(
    new AxiosError("conflict", "409", undefined, undefined, {
      status: 409,
      statusText: "Conflict",
      headers: {},
      config: { headers: new AxiosHeaders() },
      data: {
        detail: {
          code: "OPENING_EXISTS",
          message: "Este producto ya tiene stock inicial cargado.",
          origen: "manual",
          caminos: [
            "Si la fecha está mal, corregila con «Confirmar stock inicial».",
            "Si la cantidad está mal, ajustá el stock desde la edición del producto.",
          ],
        },
      },
    }),
  );
  const dialogo = await abrirEditor();
  fireEvent.click(within(dialogo).getByLabelText(/Es mi stock inicial/));
  fireEvent.change(within(dialogo).getByLabelText("Fecha del stock inicial"), {
    target: { value: "2026-08-01" },
  });
  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));

  expect(
    await within(dialogo).findByText("Si la fecha está mal, corregila con «Confirmar stock inicial»."),
  ).toBeInTheDocument();
  expect(
    within(dialogo).getByText(
      "Si la cantidad está mal, ajustá el stock desde la edición del producto.",
    ),
  ).toBeInTheDocument();
});

test("sin costo, «Es mi stock inicial» no deja guardar", async () => {
  mockGetAllProducts.mockResolvedValue([{ ...PRODUCT, unit_cost_ars: null }]);
  const dialogo = await abrirEditor();
  fireEvent.click(within(dialogo).getByLabelText(/Es mi stock inicial/));
  fireEvent.change(within(dialogo).getByLabelText("Fecha del stock inicial"), {
    target: { value: "2026-08-01" },
  });
  expect(within(dialogo).getByText(/hace falta el precio de compra/)).toBeInTheDocument();
  expect(within(dialogo).getByRole("button", { name: "Guardar" })).toBeDisabled();
});

test("un producto que ya tiene stock inicial no ofrece la opción", async () => {
  mockGetAllProducts.mockResolvedValue([{ ...PRODUCT, needs_initial_stock: false }]);
  const dialogo = await abrirEditor();
  expect(within(dialogo).getByText("¿Por qué sube el stock?")).toBeInTheDocument();
  expect(within(dialogo).queryByLabelText(/Es mi stock inicial/)).not.toBeInTheDocument();
});
