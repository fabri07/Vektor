import "@testing-library/jest-dom";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import ProductsPage from "../page";
import { productsService } from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { fieldCatalogService } from "@/services/fieldCatalog.service";
import { useAuthStore } from "@/stores/authStore";

/**
 * B·3: la categoría inferida del nombre es SÓLO sugerencia (decisión del dueño,
 * 2026-10-07). Un producto sin categoría muestra «Sugerida: <label>» y «Aceptar»
 * la fija con el PATCH de producto de siempre, mandando sólo la categoría.
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
const mockUpdateProduct = productsService.updateProduct as jest.Mock;
const mockGetCategories = productsService.getCategories as jest.Mock;
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

beforeEach(() => {
  jest.clearAllMocks();
  mockGetAllProducts.mockResolvedValue([PRODUCT]);
  mockCountProducts.mockResolvedValue(1);
  mockGetCategories.mockResolvedValue([{ code: "ILUMINACION", label: "Iluminación" }]);
  mockUpdateProduct.mockResolvedValue({ ...PRODUCT, category: "ILUMINACION", category_suggestion: null });
  mockGetFieldDefs.mockResolvedValue([]);
  mockGetAvailable.mockResolvedValue([]);
  useAuthStore.setState({
    user: { id: "u1", email: "a@a.com", full_name: "A", role: "OWNER", tenant_id: "t1" },
  });
});

function renderizar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProductsPage />
    </QueryClientProvider>,
  );
}

test("sin categoría muestra la sugerida y «Aceptar» la fija con el PATCH existente", async () => {
  renderizar();
  await waitFor(() => expect(screen.getByText("Sugerida: Iluminación")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: /aceptar categoría sugerida/i }));

  await waitFor(() =>
    expect(mockUpdateProduct).toHaveBeenCalledWith("p1", { category: "ILUMINACION" }),
  );
});

test("con categoría puesta no se ofrece sugerencia", async () => {
  mockGetAllProducts.mockResolvedValue([
    { ...PRODUCT, category: "ILUMINACION", category_suggestion: null },
  ]);
  renderizar();
  await waitFor(() => expect(screen.getByText("Lámpara de pie")).toBeInTheDocument());
  expect(screen.queryByText(/Sugerida:/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /aceptar categoría sugerida/i })).not.toBeInTheDocument();
});
