import "@testing-library/jest-dom";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import ProductsPage from "../page";
import { productsService } from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { fieldCatalogService, type AvailableField } from "@/services/fieldCatalog.service";
import { useAuthStore } from "@/stores/authStore";

/**
 * Caso real que motivó la revisión del plan (docs/plans/conservacion-y-
 * acceso-datos-negocio.md): Color/Estilo (campo del rubro sin columna real)
 * tienen que verse en la tabla de Productos, vía el catálogo de lectura —
 * sin duplicar las columnas que la página ya arma a mano.
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
const mockGetCategories = productsService.getCategories as jest.Mock;
const mockGetFieldDefs = fieldDefinitionsService.getAll as jest.Mock;
const mockGetAvailable = fieldCatalogService.getAvailableFields as jest.Mock;

const PRODUCT = {
  id: "p1",
  tenant_id: "t1",
  name: "Alfombra nórdica",
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
  custom_fields: { color: "Rojo", style: "moderno", marca: "El pasillo" },
};

function campo(overrides: Partial<AvailableField>): AvailableField {
  return {
    field_id: "product:x",
    entity_type: "product",
    field_key: "x",
    label: "X",
    data_type: "text",
    unit: null,
    origin: "canonical",
    value_path: "x",
    enum_options: null,
    editable: true,
    exportable: true,
    searchable: true,
    default_visible: true,
    enabled_for_new_entries: true,
    financial_rule: null,
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetAllProducts.mockResolvedValue([PRODUCT]);
  mockCountProducts.mockResolvedValue(1);
  mockGetCategories.mockResolvedValue([]);
  mockGetFieldDefs.mockResolvedValue([]);
  mockGetAvailable.mockResolvedValue([
    campo({
      field_id: "product:name",
      field_key: "name",
      label: "Nombre",
      value_path: "name",
    }),
    campo({
      field_id: "product:unit_cost_ars",
      field_key: "unit_cost_ars",
      label: "Costo unitario",
      value_path: "unit_cost_ars",
    }),
    campo({
      field_id: "product:custom_fields.color",
      field_key: "color",
      label: "Color",
      value_path: "custom_fields.color",
      default_visible: true,
    }),
    campo({
      field_id: "product:custom_fields.style",
      field_key: "style",
      label: "Estilo",
      value_path: "custom_fields.style",
      data_type: "enum",
      enum_options: [{ value: "moderno", label: "Moderno" }],
      default_visible: true,
    }),
  ]);
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

test("Color y Estilo aparecen en la tabla sin duplicar Costo unitario", async () => {
  renderizar();

  await waitFor(() => expect(screen.getByText("Alfombra nórdica")).toBeInTheDocument());

  // Color/Estilo son campos del rubro sin columna real: antes de este fix
  // desaparecían del todo (is_base_field descartado en buildCustomFieldColumns).
  await waitFor(() => expect(screen.getByText("Rojo")).toBeInTheDocument());
  expect(screen.getByText("Moderno")).toBeInTheDocument();

  // "Costo unitario" sigue siendo la columna hardcodeada de siempre — una
  // sola vez, no duplicada por el catálogo dinámico (está en EXCLUDED_FIELD_IDS).
  const tabla = screen.getByRole("table");
  expect(within(tabla).getAllByText(/costo unitario/i)).toHaveLength(1);
});

test("«Ver todos los datos» abre el detalle con Marca (adicional)", async () => {
  renderizar();
  await waitFor(() => expect(screen.getByText("Alfombra nórdica")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: /ver todos los datos/i }));

  await waitFor(() =>
    expect(screen.getByText("Todos los datos guardados de este registro, estén o no visibles en la tabla.")).toBeInTheDocument(),
  );
});

test("«Exportar todo» (Cambio 3) está disponible junto al selector de columnas", async () => {
  renderizar();
  await waitFor(() => expect(screen.getByText("Alfombra nórdica")).toBeInTheDocument());
  expect(screen.getByRole("button", { name: /exportar todo/i })).toBeInTheDocument();
});
