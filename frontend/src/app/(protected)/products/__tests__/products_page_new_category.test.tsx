import "@testing-library/jest-dom";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders } from "axios";

import ProductsPage from "../page";
import { productsService } from "@/services/products.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { fieldCatalogService } from "@/services/fieldCatalog.service";
import { useAuthStore } from "@/stores/authStore";

/**
 * B·3b: «+ Nueva categoría» en el editor de producto. Usa el endpoint de
 * siempre (POST /products/custom-categories, OWNER/ADMIN); la creada queda
 * seleccionada y se invalida el catálogo. Si el label coincide con una
 * canónica, el servidor devuelve ESA y es la que queda seleccionada.
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

/** Abre el editor desde la sugerencia de B·3 («Otra»): el mismo control. */
async function abrirEditorDesdeSugerencia() {
  await waitFor(() => expect(screen.getByText("Sugerida: Iluminación")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /elegir otra categoría/i }));
  const dialogo = await screen.findByRole("dialog");
  const select = within(dialogo).getByLabelText("Categoría") as HTMLSelectElement;
  return { dialogo, select };
}

async function crear(dialogo: HTMLElement, select: HTMLSelectElement, label: string) {
  fireEvent.change(select, { target: { value: "__nueva_categoria__" } });
  fireEvent.change(within(dialogo).getByLabelText("Nombre de la nueva categoría"), {
    target: { value: label },
  });
  fireEvent.click(within(dialogo).getByRole("button", { name: "Crear categoría" }));
}

test("crear una categoría la deja seleccionada, invalida el catálogo y se guarda con el producto", async () => {
  mockCreateCategory.mockResolvedValue({ code: "CUSTOM_VELAS", label: "Velas" });
  const { invalidar } = renderizar();
  const { dialogo, select } = await abrirEditorDesdeSugerencia();

  await crear(dialogo, select, "  Velas  ");

  await waitFor(() => expect(select.value).toBe("CUSTOM_VELAS"));
  expect(mockCreateCategory).toHaveBeenCalledWith("Velas");
  expect(invalidar).toHaveBeenCalledWith({ queryKey: ["product-categories"] });
  expect(within(select).getByRole("option", { name: "Velas" })).toBeInTheDocument();

  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));
  await waitFor(() =>
    expect(mockUpdateProduct).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ category: "CUSTOM_VELAS" }),
    ),
  );
  // El stock no se tocó: no viaja (el servidor lo asignaría en absoluto y
  // pisaría con el valor de la fila las ventas hechas desde que se cargó).
  expect(mockUpdateProduct.mock.calls[0][1]).not.toHaveProperty("stock_units");
});

test("si el usuario cambia el stock en el editor, sí viaja", async () => {
  renderizar();
  const { dialogo } = await abrirEditorDesdeSugerencia();
  fireEvent.change(within(dialogo).getByLabelText(/^Stock/), { target: { value: "8" } });
  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));
  await waitFor(() =>
    expect(mockUpdateProduct).toHaveBeenCalledWith("p1", expect.objectContaining({ stock_units: 8 })),
  );
});

test("Enter en el nombre crea la categoría y no envía el formulario del producto", async () => {
  mockCreateCategory.mockResolvedValue({ code: "CUSTOM_VELAS", label: "Velas" });
  renderizar();
  const { dialogo, select } = await abrirEditorDesdeSugerencia();
  fireEvent.change(select, { target: { value: "__nueva_categoria__" } });
  const input = within(dialogo).getByLabelText("Nombre de la nueva categoría");
  fireEvent.change(input, { target: { value: "Velas" } });

  // false = se llamó a preventDefault: jsdom no hace el submit implícito,
  // así que es la única aserción que detecta perder la guarda.
  expect(fireEvent.keyDown(input, { key: "Enter" })).toBe(false);
  await waitFor(() => expect(select.value).toBe("CUSTOM_VELAS"));
  expect(mockCreateCategory).toHaveBeenCalledWith("Velas");
  expect(mockUpdateProduct).not.toHaveBeenCalled();
});

test("con el alta de categoría abierta, Guardar queda deshabilitado", async () => {
  renderizar();
  const { dialogo, select } = await abrirEditorDesdeSugerencia();
  fireEvent.change(select, { target: { value: "__nueva_categoria__" } });
  fireEvent.change(within(dialogo).getByLabelText("Nombre de la nueva categoría"), {
    target: { value: "Velas" },
  });
  const guardar = within(dialogo).getByRole("button", { name: "Guardar" });
  expect(guardar).toBeDisabled();
  expect(within(dialogo).getByText(/creá o cancelá la categoría nueva/i)).toBeInTheDocument();
  fireEvent.submit(guardar.closest("form") as HTMLFormElement);
  expect(mockUpdateProduct).not.toHaveBeenCalled();
  expect(mockCreateCategory).not.toHaveBeenCalled();
});

test("la categoría creada para un producto no se aplica a otro abierto después", async () => {
  const OTRO = { ...PRODUCT, id: "p2", name: "Mesa ratona" };
  mockGetAllProducts.mockResolvedValue([PRODUCT, OTRO]);
  let responder: (v: { code: string; label: string }) => void = () => {};
  mockCreateCategory.mockReturnValue(new Promise((r) => (responder = r)));
  renderizar();
  await waitFor(() => expect(screen.getAllByText("Sugerida: Iluminación")).toHaveLength(2));

  fireEvent.click(screen.getByRole("button", { name: /elegir otra categoría para lámpara/i }));
  let dialogo = await screen.findByRole("dialog");
  await crear(dialogo, within(dialogo).getByLabelText("Categoría") as HTMLSelectElement, "Fiambres");
  expect(mockCreateCategory).toHaveBeenCalledWith("Fiambres");
  // Antes de la respuesta, cierra y abre otro producto.
  fireEvent.click(within(dialogo).getByRole("button", { name: "Cancelar" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /elegir otra categoría para mesa/i }));
  dialogo = await screen.findByRole("dialog");
  const select = within(dialogo).getByLabelText("Categoría") as HTMLSelectElement;
  expect(within(dialogo).getByLabelText("Producto")).toHaveValue("Mesa ratona");

  responder({ code: "CUSTOM_FIAMBRES", label: "Fiambres" });
  // La respuesta llegó (la creada aparece en el catálogo) pero no se elige sola.
  await within(select).findByRole("option", { name: "Fiambres" });
  await waitFor(() => expect(within(dialogo).getByRole("button", { name: "Guardar" })).toBeEnabled());
  expect(select.value).toBe("");

  fireEvent.click(within(dialogo).getByRole("button", { name: "Guardar" }));
  await waitFor(() => expect(mockUpdateProduct).toHaveBeenCalled());
  expect(mockUpdateProduct).toHaveBeenCalledWith("p2", expect.objectContaining({ category: null }));
});

test("un label que coincide con una canónica deja seleccionada la canónica", async () => {
  mockCreateCategory.mockResolvedValue({ code: "ILUMINACION", label: "Iluminación" });
  renderizar();
  const { dialogo, select } = await abrirEditorDesdeSugerencia();

  await crear(dialogo, select, "iluminacion");

  await waitFor(() => expect(select.value).toBe("ILUMINACION"));
  // No aparece una segunda «Iluminación» en el desplegable.
  expect(within(select).getAllByRole("option", { name: "Iluminación" })).toHaveLength(1);
});

test("un rol sin permiso no ve la opción de crear", async () => {
  comoRol("VIEWER");
  renderizar();
  const { select } = await abrirEditorDesdeSugerencia();
  expect(within(select).queryByRole("option", { name: "+ Nueva categoría" })).not.toBeInTheDocument();
});

test("el error del servidor se muestra con su mensaje y no cambia la selección", async () => {
  mockCreateCategory.mockRejectedValue(
    new AxiosError("Bad Request", "ERR_BAD_REQUEST", undefined, undefined, {
      status: 400,
      statusText: "Bad Request",
      data: { detail: "No se pudo crear la categoría: nombre reservado." },
      headers: {},
      config: { headers: new AxiosHeaders() },
    }),
  );
  renderizar();
  const { dialogo, select } = await abrirEditorDesdeSugerencia();

  await crear(dialogo, select, "Velas");

  expect(await within(dialogo).findByRole("alert")).toHaveTextContent(
    "No se pudo crear la categoría: nombre reservado.",
  );
  fireEvent.click(within(dialogo).getByRole("button", { name: "Cancelar nueva categoría" }));
  expect(select.value).toBe("");
});
