import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AllDataModal } from "../AllDataModal";
import { fieldCatalogService, type AvailableField } from "@/services/fieldCatalog.service";

/**
 * Caso real que motivó la revisión del plan: Color/Estilo (campo del rubro
 * sin columna real), Marca (recuperada por backfill, no editable todavía) y
 * un adicional deshabilitado para nuevas cargas tienen que verse en el
 * detalle con su valor y procedencia correctos.
 */

jest.mock("@/services/fieldCatalog.service", () => ({
  fieldCatalogService: { getAvailableFields: jest.fn() },
}));

const mockGetAvailable = fieldCatalogService.getAvailableFields as jest.Mock;

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

function renderizar(row: object) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AllDataModal
        entityType="product"
        title="Silla de living"
        row={row}
        isOpen={true}
        onClose={jest.fn()}
      />
    </QueryClientProvider>,
  );
}

test("Color, Estilo y Marca se ven con su valor y procedencia", async () => {
  mockGetAvailable.mockResolvedValue([
    campo({
      field_id: "product:custom_fields.color",
      field_key: "color",
      label: "Color",
      value_path: "custom_fields.color",
      origin: "canonical",
    }),
    campo({
      field_id: "product:custom_fields.style",
      field_key: "style",
      label: "Estilo",
      value_path: "custom_fields.style",
      origin: "canonical",
      data_type: "enum",
      enum_options: [{ value: "moderno", label: "Moderno" }],
    }),
    campo({
      field_id: "product:custom_fields.marca",
      field_key: "marca",
      label: "Marca",
      value_path: "custom_fields.marca",
      origin: "additional",
      editable: false,
    }),
  ]);

  renderizar({ custom_fields: { color: "Rojo", style: "moderno", marca: "El pasillo" } });

  await waitFor(() => expect(screen.getByText("Color")).toBeInTheDocument());
  expect(screen.getByText("Rojo")).toBeInTheDocument();
  expect(screen.getByText("Moderno")).toBeInTheDocument();
  expect(screen.getByText("El pasillo")).toBeInTheDocument();
  expect(screen.getAllByText("Campo del negocio").length).toBeGreaterThan(0);
  expect(screen.getByText("Campo adicional")).toBeInTheDocument();
});

test("un campo de evidencia deshabilitado para nuevas cargas se avisa", async () => {
  mockGetAvailable.mockResolvedValue([
    campo({
      field_id: "product:custom_fields.purchase_base_cost",
      field_key: "purchase_base_cost",
      label: "Precio de compra (costo base, sin envío)",
      value_path: "custom_fields.purchase_base_cost",
      origin: "evidence",
      data_type: "number",
      unit: "ARS",
      editable: false,
      financial_rule: "Auxiliar del costo original del archivo.",
    }),
  ]);

  renderizar({ custom_fields: { purchase_base_cost: "8500" } });

  await waitFor(() =>
    expect(screen.getByText(/precio de compra/i)).toBeInTheDocument(),
  );
  expect(screen.getByText("8.500")).toBeInTheDocument();
  expect(screen.getByText("Evidencia del archivo importado")).toBeInTheDocument();
  expect(screen.getByText("Auxiliar del costo original del archivo.")).toBeInTheDocument();
});

test("un campo deshabilitado para nuevas cargas se marca, pero sigue visible", async () => {
  mockGetAvailable.mockResolvedValue([
    campo({
      field_id: "product:custom_fields.viejo",
      field_key: "viejo",
      label: "Campo viejo",
      value_path: "custom_fields.viejo",
      origin: "additional",
      enabled_for_new_entries: false,
    }),
  ]);

  renderizar({ custom_fields: { viejo: "un valor histórico" } });

  await waitFor(() => expect(screen.getByText("un valor histórico")).toBeInTheDocument());
  expect(screen.getByText("No disponible para nuevas cargas")).toBeInTheDocument();
});

test("un valor ausente se ve como guión, no se cae", async () => {
  mockGetAvailable.mockResolvedValue([
    campo({
      field_id: "product:custom_fields.sin_dato",
      field_key: "sin_dato",
      label: "Sin dato",
      value_path: "custom_fields.sin_dato",
    }),
  ]);

  renderizar({ custom_fields: {} });

  await waitFor(() => expect(screen.getByText("Sin dato")).toBeInTheDocument());
  expect(screen.getByText("—")).toBeInTheDocument();
});
