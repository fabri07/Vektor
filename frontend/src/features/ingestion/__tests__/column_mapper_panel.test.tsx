import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ColumnMapperPanel, splitWarningsByContext } from "../ColumnMapperPanel";
import {
  ingestionService,
  type ColumnMapping,
  type ColumnRiskDecision,
  type ContextualColumnRisk,
} from "@/services/ingestion.service";

const mockAddToast = jest.fn();
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: mockAddToast }),
}));

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    getPreview: jest.fn(),
    getColumnMappings: jest.fn(),
    getFieldCatalog: jest.fn(),
    confirmFile: jest.fn(),
    cancelFile: jest.fn(),
    recomputeColumnRisk: jest.fn(),
    fetchInventoryEffects: jest.fn(),
    fetchPurchaseGroups: jest.fn(),
  },
}));

const mockGetPreview = ingestionService.getPreview as jest.Mock;
const mockGetColumnMappings = ingestionService.getColumnMappings as jest.Mock;
const mockGetFieldCatalog = ingestionService.getFieldCatalog as jest.Mock;
const mockConfirmFile = ingestionService.confirmFile as jest.Mock;
const mockRecomputeColumnRisk = ingestionService.recomputeColumnRisk as jest.Mock;
const mockInventoryEffects = ingestionService.fetchInventoryEffects as jest.Mock;
const mockPurchaseGroups = ingestionService.fetchPurchaseGroups as jest.Mock;

/**
 * F-F.4 — lo que el backend DEDUCE para una hoja de ventas de mercadería. El
 * texto lo sirve `/inventory-effects`: el frontend no tiene lista propia, y
 * desde F-F.4 tampoco tiene qué elegir — viene una sola opción.
 */
const EFECTOS_VENTAS = [
  {
    context_id: "table",
    label: "Ventas",
    default: "historical_replay",
    options: [
      {
        value: "historical_replay",
        label: "Las compras suman y las ventas restan del inventario",
      },
    ],
  },
];

// Los selects se arman con lo que devuelve el backend: el frontend dejó de tener
// su propia copia (divergió y hacía que la UI mostrara "Sin mapear" sobre un
// target real). Espeja CANONICAL_FIELDS + REQUIRED_FIELDS + SINGLE_VALUE_FIELDS.
const FIELD_CATALOG = {
  sale: {
    required: ["amount", "transaction_date"],
    fields: [
      {
        value: "amount",
        label: "Monto de venta",
        single_value: true,
        // F-C: el motivo lo escribe el backend, como consecuencia de una regla
        // del importador. Acá se espeja para poder verificar que la pantalla lo
        // muestre en vez de un asterisco rojo.
        required_reason:
          "Véktor necesita saber cuánta plata entró. La fila que no lo traiga queda en «Otros».",
      },
      {
        value: "transaction_date",
        label: "Fecha de venta",
        single_value: true,
        required_reason:
          "Es lo que ubica cada venta en su período. La fila con una fecha ilegible queda en «Otros» — nunca se le pone la de hoy.",
      },
      { value: "quantity", label: "Cantidad", single_value: true },
      { value: "unit_price", label: "Precio unitario vendido", single_value: true },
      { value: "payment_method", label: "Método de pago", single_value: false },
      { value: "product_name", label: "Nombre del producto", single_value: false },
      { value: "notes", label: "Notas", single_value: false },
      { value: "customer_dni", label: "Cliente — DNI", single_value: false },
      { value: "customer_cuit", label: "Cliente — CUIT", single_value: false },
      { value: "customer_email", label: "Cliente — Email", single_value: false },
      { value: "customer_phone", label: "Cliente — Teléfono", single_value: false },
      { value: "customer_name", label: "Cliente — Nombre", single_value: false },
    ],
  },
  expense: {
    required: ["amount", "expense_date"],
    fields: [
      { value: "amount", label: "Monto del gasto", single_value: true },
      { value: "expense_date", label: "Fecha del gasto", single_value: true },
      { value: "category", label: "Categoría", single_value: false },
      { value: "payment_method", label: "Método de pago", single_value: false },
      { value: "is_recurring", label: "Recurrente", single_value: false },
      { value: "invoice_number", label: "Número de comprobante", single_value: true },
      { value: "shipping_cost", label: "Envío / flete", single_value: true },
      {
        value: "shipping_cost_line",
        label: "Envío ya asignado a esta línea",
        single_value: true,
      },
      { value: "supplier_name", label: "Proveedor", single_value: false },
      { value: "notes", label: "Notas", single_value: false },
      { value: "supplier_cuil", label: "Proveedor — CUIL", single_value: false },
      { value: "supplier_email", label: "Proveedor — Email", single_value: false },
      { value: "supplier_phone", label: "Proveedor — Teléfono", single_value: false },
    ],
  },
  product: {
    required: ["name"],
    fields: [
      { value: "sku", label: "Código (SKU)", single_value: false },
      { value: "barcode", label: "Código de barras (EAN/UPC)", single_value: false },
      { value: "name", label: "Nombre", single_value: false },
      { value: "sale_price_ars", label: "Precio de venta", single_value: true },
      {
        value: "list_price_ars",
        label: "Precio de lista (sugerido)",
        single_value: true,
      },
      { value: "unit_cost_ars", label: "Costo unitario", single_value: true },
      { value: "stock_units", label: "Stock (unidades)", single_value: true },
      { value: "category", label: "Categoría", single_value: false },
      { value: "description", label: "Descripción", single_value: false },
    ],
  },
  customer: {
    required: ["name"],
    fields: [{ value: "name", label: "Nombre", single_value: false }],
  },
  supplier: {
    required: ["name"],
    fields: [{ value: "name", label: "Nombre", single_value: false }],
  },
};

// Helper: arma un ContextualColumnRisk completo (los tests solo pisan lo que importa).
function makeContextualRisk(
  overrides: Partial<ContextualColumnRisk> = {},
): ContextualColumnRisk {
  return {
    context_id: "table",
    entity_type: "sale",
    source_column: "obs",
    target_field: "notes",
    null_ratio: 0.9,
    affected_rows: 45,
    null_rows: 45,
    invalid_rows: 0,
    field_requirement: "optional",
    mapping_source: "heuristic",
    user_selected: false,
    allowed_actions: ["route_affected_rows_to_others", "drop_column"],
    recommendation: "Revisá o eliminá la columna",
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ColumnMapperPanel fileId="file-1" onDone={jest.fn()} />
    </QueryClientProvider>,
  );
}

describe("ColumnMapperPanel — A3 clarificación inline", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetColumnMappings.mockResolvedValue([]);
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    // Default: el recompute no cambia el set (evita vaciar el panel si el
    // debounce llega a dispararse durante un test).
    mockRecomputeColumnRisk.mockResolvedValue([]);
    // Sin hojas: los tests que no miran el inventario no renderizan el selector.
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
  });

  test("archivo ambiguo (general) muestra el selector de propósito", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "general", headers: ["a", "b"] },
      columns_at_risk: [],
    });

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Revisá antes de confirmar/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/No pudimos determinar/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ventas" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Gastos" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Productos/i })).toBeInTheDocument();
    // No se piden mapeos hasta elegir el propósito.
    expect(mockGetColumnMappings).not.toHaveBeenCalled();
  });

  test("renderiza contextual_column_risk del preview (null_ratio → %)", async () => {
    // null_ratio 0.9 debe mostrarse como 90% (NO 1%, el bug legacy de Math.round).
    const risk = makeContextualRisk({ source_column: "obs", null_ratio: 0.9 });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["fecha", "obs"] },
      columns_at_risk: [],
      contextual_column_risk: [risk],
    });
    mockRecomputeColumnRisk.mockResolvedValue([risk]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Columnas con muchos datos vacíos/i)).toBeInTheDocument();
    });
    expect(screen.getByText("obs")).toBeInTheDocument();
    expect(screen.getByText(/90% vacío/i)).toBeInTheDocument();
    expect(screen.queryByText(/^1% vacío/)).not.toBeInTheDocument();
  });

  test("el source 'llm' se cuenta como «Sugerido por Véktor», sin porcentaje", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["ColX"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.8,
        source: "llm",
        status: "mapped",
      },
    ]);

    renderPanel();

    // F-B.1: la capa que resolvió el mapeo se cuenta en castellano y sin el
    // porcentaje, que no medía nada.
    await waitFor(() => {
      expect(screen.getByText("Sugerido por Véktor")).toBeInTheDocument();
    });
    expect(screen.queryByText(/80\s*%/)).not.toBeInTheDocument();
  });

  test("confirm con 409 → toast amable (ya se está importando / ya se importó)", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      // Fecha + Monto cubren los requeridos de `sale`; ColX es la columna
      // opcional sobre la que se prueba `user_selected`. Sin los requeridos
      // cubiertos el panel bloquea el confirm — igual que el backend, que
      // devuelve 422 (`Campos requeridos sin mapear`).
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["ColX", "Fecha", "Monto"],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["algo"],
        target_field: "notes",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["01/02/2026"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    // El confirm concurrente / archivo ya importado devuelve 409 (forma AxiosError).
    mockConfirmFile.mockRejectedValue({ response: { status: 409 } });

    renderPanel();

    const confirmBtn = await screen.findByRole("button", {
      name: /Confirmar importación/i,
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockAddToast).toHaveBeenCalledWith(
        "Ya se está importando este archivo o ya se importó — actualizá la lista",
        "warning",
      );
    });
    // El 409 NO debe pintar el banner rojo de "error de mapeo".
    expect(
      screen.queryByText(/Error al confirmar/i),
    ).not.toBeInTheDocument();
  });

  test("confirm con timeout (ECONNABORTED) → toast informativo, sin error rojo", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      // Fecha + Monto cubren los requeridos de `sale`; ColX es la columna
      // opcional sobre la que se prueba `user_selected`. Sin los requeridos
      // cubiertos el panel bloquea el confirm — igual que el backend, que
      // devuelve 422 (`Campos requeridos sin mapear`).
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["ColX", "Fecha", "Monto"],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["algo"],
        target_field: "notes",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["01/02/2026"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    // Timeout del cliente axios: el import sigue en curso en el backend.
    mockConfirmFile.mockRejectedValue({ code: "ECONNABORTED" });

    renderPanel();

    const confirmBtn = await screen.findByRole("button", {
      name: /Confirmar importación/i,
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockAddToast).toHaveBeenCalledWith(
        "El import sigue en curso — revisá la lista en unos segundos",
        "info",
      );
    });
    expect(
      screen.queryByText(/Error al confirmar/i),
    ).not.toBeInTheDocument();
  });

  test("F7e: campos de referencia al cliente disponibles al mapear una hoja de ventas", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["documento"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "documento",
        normalized_column: "documento",
        sample_values: ["30111222"],
        target_field: null,
        confidence: 0,
        source: "none",
        status: "unmapped",
      },
    ]);

    renderPanel();

    await screen.findAllByText("documento");
    // Debe poder mapearse a los campos de referencia al cliente (mismos
    // target_field que column_mapping_service.CANONICAL_FIELDS["sale"] en el backend).
    // La columna sin mapear aparece tanto en el bloque "revisá antes de
    // confirmar" como en la tabla principal — de ahí *AllBy*.
    expect(screen.getAllByRole("option", { name: "Cliente — DNI" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "Cliente — CUIT" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "Cliente — Email" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "Cliente — Teléfono" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "Cliente — Nombre" }).length).toBeGreaterThan(0);
  });

  test("F7e: campos de referencia al proveedor disponibles al mapear una hoja de gastos", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "gastos", headers: ["contacto"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "contacto",
        normalized_column: "contacto",
        sample_values: ["20333444"],
        target_field: null,
        confidence: 0,
        source: "none",
        status: "unmapped",
      },
    ]);

    renderPanel();

    await screen.findAllByText("contacto");
    expect(screen.getAllByRole("option", { name: "Proveedor — CUIL" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "Proveedor — Email" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "Proveedor — Teléfono" }).length).toBeGreaterThan(0);
    // "Proveedor" (supplier_name) ya existía antes de F7e — sigue disponible.
    expect(screen.getAllByRole("option", { name: "Proveedor" }).length).toBeGreaterThan(0);
  });

  test("F7e: los buckets Clientes/Proveedores aparecen como checkbox de importación", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["a"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("clientes")).toBeInTheDocument();
    });
    expect(screen.getByText("proveedores")).toBeInTheDocument();
  });

  test("F7e: preview de maestros — conteos por bucket y muestra needs_review/invalid", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["a"] },
      columns_at_risk: [],
      master_previews: [
        {
          context_id: null,
          entity_type: "customer",
          to_create: 3,
          to_update: 1,
          needs_review: 2,
          invalid: 1,
          duplicates: 0,
          samples: [
            {
              row_index: 0,
              status: "needs_review",
              display_name: "Juan Pérez",
              existing_name: null,
              issue: "Sin documento ni email para identificar",
            },
          ],
        },
      ],
    });
    mockGetColumnMappings.mockResolvedValue([]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Clientes")).toBeInTheDocument();
    });
    expect(screen.getByText("En revisión")).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Ver por qué/i));
    expect(screen.getByText(/Juan Pérez/)).toBeInTheDocument();
    expect(screen.getByText(/Sin documento ni email para identificar/)).toBeInTheDocument();
  });

  test("multi-contexto: muestra contextual_column_risk y source llm por hoja", async () => {
    const risk = makeContextualRisk({
      context_id: "hoja1",
      source_column: "obs",
      null_ratio: 0.8,
    });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "mixed",
        mapping_contexts: [
          {
            context_id: "hoja1",
            label: "Ventas",
            source_kind: "table",
            entity_type: "sale",
            headers: ["ColX"],
            fields: null,
            preview_rows: [{ ColX: "1500" }],
            row_count: 1,
          },
          {
            context_id: "hoja2",
            label: "Gastos",
            source_kind: "table",
            entity_type: "expense",
            headers: ["ColY"],
            fields: null,
            preview_rows: [{ ColY: "300" }],
            row_count: 1,
          },
        ],
      },
      columns_at_risk: [],
      contextual_column_risk: [risk],
    });
    mockRecomputeColumnRisk.mockResolvedValue([risk]);
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.77,
        source: "llm",
        status: "mapped",
      },
    ]);

    renderPanel();

    // El panel de riesgo aparece también en multi-hoja.
    await waitFor(() => {
      expect(screen.getByText(/Columnas con muchos datos vacíos/i)).toBeInTheDocument();
    });
    expect(screen.getByText("obs")).toBeInTheDocument();
    expect(screen.getByText(/80% vacío/i)).toBeInTheDocument();
    // source llm visible en al menos una sección de hoja, contado en castellano
    // y sin el porcentaje de confianza (F-B.1).
    await waitFor(() => {
      expect(screen.getAllByText("Sugerido por Véktor").length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/77\s*%/)).not.toBeInTheDocument();
  });

  test("user_selected: sugerencia inicial NO cuenta como manual; cambiar el mapeo sí", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      // Fecha + Monto cubren los requeridos de `sale`; ColX es la columna
      // opcional sobre la que se prueba `user_selected`. Sin los requeridos
      // cubiertos el panel bloquea el confirm — igual que el backend, que
      // devuelve 422 (`Campos requeridos sin mapear`).
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["ColX", "Fecha", "Monto"],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["algo"],
        target_field: "notes",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["01/02/2026"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });

    renderPanel();

    // Esperar a que carguen las sugerencias (la columna y su select).
    await screen.findAllByText("ColX");

    // Confirmar SIN tocar el mapeo auto-sugerido.
    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );
    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    const first = mockConfirmFile.mock.calls[0][2] as ColumnMapping[];
    const colxInitial = first.find((m) => m.source_column === "ColX");
    // La sugerencia inicial NO es una selección manual.
    expect(colxInitial?.user_selected).toBeFalsy();

    // Ahora el usuario cambia el mapeo manualmente y reconfirma.
    mockConfirmFile.mockClear();
    fireEvent.change(screen.getAllByRole("combobox")[0]!, {
      target: { value: "quantity" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );
    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    const second = mockConfirmFile.mock.calls[0][2] as ColumnMapping[];
    const colxManual = second.find((m) => m.source_column === "ColX");
    expect(colxManual?.user_selected).toBe(true);
  });

  test("las decisiones de riesgo viajan al confirm", async () => {
    const risk = makeContextualRisk({
      context_id: "table",
      source_column: "obs",
      target_field: "notes",
      allowed_actions: ["route_affected_rows_to_others"],
    });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["obs", "Fecha", "Monto"],
      },
      columns_at_risk: [],
      contextual_column_risk: [risk],
    });
    mockRecomputeColumnRisk.mockResolvedValue([risk]);
    // Fecha + Monto cubren los requeridos de `sale`: sin eso el panel bloquea
    // el confirm, igual que el 422 del backend.
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "obs",
        normalized_column: "obs",
        sample_values: ["x"],
        target_field: "notes",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["01/02/2026"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });

    renderPanel();

    // Elegir "enviar filas afectadas a Otros" en el panel de decisiones.
    const routeBtn = await screen.findByRole("button", {
      name: /Enviar filas afectadas a Otros/i,
    });
    fireEvent.click(routeBtn);
    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // column_risk_decisions es el 7º arg posicional de confirmFile (índice 6).
    const decisions = mockConfirmFile.mock.calls[0][6] as ColumnRiskDecision[];
    expect(decisions).toEqual([
      {
        context_id: "table",
        source_column: "obs",
        target_field: "notes",
        action: "route_affected_rows_to_others",
      },
    ]);
  });

  /**
   * F-F.4 — el panel dejó de mandar `inventory_effect`.
   *
   * F-H3.e lo había cableado justamente porque sin eso `historical_replay` era
   * inalcanzable desde la pantalla. Ahora el backend lo deduce del mismo mapeo
   * que este confirm ya manda, así que repetirlo desde acá sería tener dos
   * fuentes de la misma regla — y la que se desincroniza es siempre la del
   * frontend (el catálogo de campos, el incidente ASTERIA).
   */
  test("el efecto de inventario ya no viaja en el confirm", async () => {
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["Fecha", "Monto"],
        mapping_contexts: [
          {
            context_id: "table",
            label: "Tabla",
            source_kind: "table",
            entity_type: "sale",
            headers: ["Fecha", "Monto"],
            fields: null,
            preview_rows: [],
            row_count: 1,
          },
        ],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-10"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    mockInventoryEffects.mockResolvedValue(EFECTOS_VENTAS);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });

    renderPanel();

    // La línea informativa ya está: el efecto llegó del backend y se muestra.
    await screen.findByText(EFECTOS_VENTAS[0]!.options[0]!.label);
    // Y no hay nada que elegir.
    expect(
      screen.queryByRole("button", { name: EFECTOS_VENTAS[0]!.options[0]!.label }),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // `inventory_effect` es el 8º argumento posicional (índice 7).
    expect(mockConfirmFile.mock.calls[0][7]).toBeUndefined();
  });

  test("la pantalla muestra el efecto que dedujo el backend", async () => {
    // El texto sale del backend y no de una tabla de acá: es la misma regla que
    // decide el import, así que una copia local puede mostrar lo que no pasa.
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["Fecha", "Monto"],
        mapping_contexts: [
          {
            context_id: "table",
            label: "Tabla",
            source_kind: "table",
            entity_type: "sale",
            headers: ["Fecha", "Monto"],
            fields: null,
            preview_rows: [],
            row_count: 1,
          },
        ],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-10"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    mockInventoryEffects.mockResolvedValue(EFECTOS_VENTAS);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });

    renderPanel();

    expect(
      await screen.findByText(EFECTOS_VENTAS[0]!.options[0]!.label),
    ).toBeInTheDocument();
  });

  test("touched-set: resetear el mapeo (checkbox de confirmedFields) limpia el touched-set", async () => {
    // Regresión: choosePurpose() y el onChange de los checkboxes de
    // confirmedFields re-inicializan `mappings` desde las sugerencias pero
    // antes NO limpiaban touchedRef — una columna tocada a mano quedaba
    // marcada user_selected=true aunque terminara re-derivada de la sugerencia.
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      // Fecha + Monto cubren los requeridos de `sale`; ColX es la columna
      // opcional sobre la que se prueba `user_selected`. Sin los requeridos
      // cubiertos el panel bloquea el confirm — igual que el backend, que
      // devuelve 422 (`Campos requeridos sin mapear`).
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["ColX", "Fecha", "Monto"],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
        sample_values: ["algo"],
        target_field: "notes",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["01/02/2026"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });

    renderPanel();

    await screen.findAllByText("ColX");

    // El usuario cambia el mapeo a mano → queda "tocado".
    fireEvent.change(screen.getAllByRole("combobox")[0]!, {
      target: { value: "quantity" },
    });

    // Togglear un checkbox de confirmedFields resetea `mappings`/`initialized`
    // (re-deriva desde la sugerencia original) — debe limpiar el touched-set.
    fireEvent.click(screen.getByRole("checkbox", { name: "gastos" }));

    // El mapeo vuelve a "notes" (la sugerencia original de ColX), no "quantity".
    await waitFor(() => {
      const select = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
      expect(select.value).toBe("notes");
    });

    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );
    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    const calls = mockConfirmFile.mock.calls[0][2] as ColumnMapping[];
    const colx = calls.find((m) => m.source_column === "ColX");
    // Re-derivada de la sugerencia tras el reset: NO es una selección manual.
    expect(colx?.user_selected).toBeFalsy();
  });

  test("el panel de decisiones aparece solo con contextual_column_risk", async () => {
    const risk = makeContextualRisk();
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["obs"] },
      columns_at_risk: [],
      contextual_column_risk: [risk],
    });
    mockRecomputeColumnRisk.mockResolvedValue([risk]);

    const { unmount } = renderPanel();
    await waitFor(() => {
      expect(
        screen.getByTestId("column-risk-decisions-panel"),
      ).toBeInTheDocument();
    });
    unmount();

    // Sin contextual_column_risk el panel no se monta.
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["obs"] },
      columns_at_risk: [],
      contextual_column_risk: [],
    });
    renderPanel();
    await waitFor(() => {
      expect(mockGetColumnMappings).toHaveBeenCalled();
    });
    expect(
      screen.queryByTestId("column-risk-decisions-panel"),
    ).not.toBeInTheDocument();
  });
});

// ── Hojas que Véktor no pudo clasificar ───────────────────────────────────────
//
// El parser deja `entity_type: null` cuando no sabe qué es una hoja. Antes el
// panel las mostraba TILDADAS y con la sección puesta en "Ventas" por un
// `?? "sale"`, así que una hoja de resúmenes derivados del Libro Diario entraba
// como miles de ventas sin que nadie lo decidiera.

function previewConHojaSinClasificar(warnings: string[] = []) {
  return {
    file_id: "file-1",
    processing_status: "NEEDS_CONFIRMATION",
    parsed_summary_json: {
      inferred_type: "mixed",
      warnings,
      mapping_contexts: [
        {
          context_id: "sheet:LD:ventas",
          label: "LD ventas",
          source_kind: "table",
          entity_type: "sale",
          headers: ["fecha"],
          fields: null,
          preview_rows: [{ fecha: "2024-01-15" }],
          row_count: 3,
        },
        {
          context_id: "sheet:Ganancias",
          label: "Ganancias",
          source_kind: "table",
          entity_type: null, // el parser no supo qué es
          headers: ["concepto"],
          fields: null,
          preview_rows: [{ concepto: "x" }],
          row_count: 1840,
        },
      ],
    },
    columns_at_risk: [],
    contextual_column_risk: [],
  };
}

describe("ColumnMapperPanel — hojas sin clasificar", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetColumnMappings.mockResolvedValue([]);
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    // Sin hojas: los tests que no miran el inventario no renderizan el selector.
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
  });

  test("no arranca en Ventas: pide elegir la sección", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaSinClasificar());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Ganancias")).toBeInTheDocument();
    });

    // El selector arranca en el placeholder, NO en "Ventas".
    const selector = screen.getByLabelText("Sección de la hoja Ganancias");
    expect(selector).toHaveValue("");
    // Y ofrece Productos, que antes ni siquiera era una opción.
    expect(
      within(selector).getByRole("option", { name: "Productos" }),
    ).toBeInTheDocument();
  });

  test("arranca destildada y no bloquea el confirm de las demás", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaSinClasificar());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Ganancias")).toBeInTheDocument();
    });

    const checkboxes = screen.getAllByRole("checkbox");
    // Hoja clasificada tildada, hoja sin clasificar destildada.
    expect(checkboxes[0]).toBeChecked();
    expect(checkboxes[1]).not.toBeChecked();

    // Con la hoja ambigua afuera, se puede confirmar.
    expect(
      screen.getByRole("button", { name: /Confirmar importación/ }),
    ).toBeEnabled();
  });

  test("tildarla sin elegir sección bloquea el confirm con el motivo", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaSinClasificar());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Ganancias")).toBeInTheDocument();
    });

    const [, hojaAmbigua] = screen.getAllByRole("checkbox");
    fireEvent.click(hojaAmbigua!);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Confirmar importación/ }),
      ).toBeDisabled();
    });
    // Hay dos mensajes que empiezan igual: el de adentro de la hoja y el del
    // pie, que es el que nombra la hoja que bloquea. "destildá" es del pie.
    const bloqueo = screen.getAllByText(/o destildá/);
    expect(bloqueo[bloqueo.length - 1]).toHaveTextContent("«Ganancias»");
  });

  test("el aviso del parser se muestra en la hoja que lo generó", async () => {
    const aviso =
      "La hoja 'Ganancias' parece derivada del Libro Diario (resumen): no se " +
      "importa automáticamente para no duplicar movimientos.";
    mockGetPreview.mockResolvedValue(previewConHojaSinClasificar([aviso]));
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(aviso)).toBeInTheDocument();
    });
  });
});

describe("splitWarningsByContext", () => {
  const contexts = [
    { context_id: "c1", label: "Ganancias" },
    { context_id: "c2", label: "precios y stock " }, // label con espacio al final
  ] as Parameters<typeof splitWarningsByContext>[1];

  test("atribuye cada aviso a la hoja que lo menciona", () => {
    const { byContext, general } = splitWarningsByContext(
      ["La hoja 'Ganancias' parece derivada del Libro Diario."],
      contexts,
    );
    expect(byContext.c1).toHaveLength(1);
    expect(byContext.c2).toBeUndefined();
    expect(general).toHaveLength(0);
  });

  test("matchea labels con espacios al final", () => {
    const { byContext } = splitWarningsByContext(
      ["Columnas vacías en 'precios y stock'."],
      contexts,
    );
    expect(byContext.c2).toHaveLength(1);
  });

  test("un aviso que no menciona ninguna hoja NO se pierde", () => {
    const { byContext, general } = splitWarningsByContext(
      ["4 movimiento(s) de 'LD 2026' son ambiguos."],
      contexts,
    );
    expect(Object.keys(byContext)).toHaveLength(0);
    expect(general).toEqual(["4 movimiento(s) de 'LD 2026' son ambiguos."]);
  });
});

// El clasificador se equivoca: en un archivo real mandó a "Productos" una hoja
// llamada Ventas (1187 filas) y otra llamada Clientes (9 filas). El selector de
// sección sólo aparecía cuando el parser NO había sabido clasificar
// (`canChooseEntity = isText || entityUnknown`), así que una hoja mal
// clasificada mostraba una chapita de sólo lectura y no había forma de
// corregirla. El reconocimiento automático sigue vigente como sugerencia — lo
// que cambia es que la persona siempre puede acomodarlo.

function previewConHojaMalClasificada() {
  return {
    file_id: "file-1",
    processing_status: "NEEDS_CONFIRMATION",
    parsed_summary_json: {
      inferred_type: "mixed",
      warnings: [],
      mapping_contexts: [
        {
          context_id: "sheet:Ventas",
          label: "Ventas",
          source_kind: "sheet",
          entity_type: "product", // el parser se equivocó
          headers: ["Fecha", "Total"],
          fields: null,
          preview_rows: [{ Fecha: "2026-05-01", Total: "1910" }],
          row_count: 1187,
        },
      ],
    },
    columns_at_risk: [],
    contextual_column_risk: [],
  };
}

describe("ColumnMapperPanel — corregir una hoja mal clasificada", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetColumnMappings.mockResolvedValue([]);
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    // Sin hojas: los tests que no miran el inventario no renderizan el selector.
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
  });

  test("la sección es un desplegable, no una chapita de sólo lectura", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaMalClasificada());
    renderPanel();

    const selector = await screen.findByLabelText("Sección de la hoja Ventas");
    expect(selector.tagName).toBe("SELECT");
    // Precargado con lo que adivinó el parser: la sugerencia sigue vigente.
    expect(selector).toHaveValue("product");
  });

  test("se puede moverla a Ventas y el confirm manda la sección corregida", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaMalClasificada());
    mockConfirmFile.mockResolvedValue({
      file_id: "file-1",
      status: "DONE",
      message: "ok",
    });
    renderPanel();

    const selector = await screen.findByLabelText("Sección de la hoja Ventas");
    fireEvent.change(selector, { target: { value: "sale" } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Confirmar importación/ })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Confirmar importación/ }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    const contextEntity = mockConfirmFile.mock.calls[0]![4];
    expect(contextEntity).toEqual({ "sheet:Ventas": "sale" });
  });

  test("Clientes y Proveedores son destinos ofrecidos", async () => {
    // Estuvieron afuera mientras `_import_master_entities` ignoraba el override:
    // elegirlos confirmaba sin error y no importaba nada. Ahora el importador
    // los honra, así que la hoja "Clientes" de la captura tiene salida.
    mockGetPreview.mockResolvedValue(previewConHojaMalClasificada());
    renderPanel();

    const selector = await screen.findByLabelText("Sección de la hoja Ventas");
    for (const seccion of ["Ventas", "Gastos", "Productos", "Clientes", "Proveedores"]) {
      expect(within(selector).getByRole("option", { name: seccion })).toBeInTheDocument();
    }
  });

  test("el conteo de filas sigue visible al lado del desplegable", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaMalClasificada());
    renderPanel();

    await screen.findByLabelText("Sección de la hoja Ventas");
    expect(screen.getByText(/1187 filas/)).toBeInTheDocument();
  });
});

/**
 * F-M — una columna que Véktor entendió y no pudo decidir.
 *
 * El backend distingue tres cosas donde antes había dos: resuelta, sin
 * reconocer, y «entendí y sigue habiendo más de una lectura». La pantalla tiene
 * que mostrar las dos últimas distinto, o la distinción no existe para nadie.
 */
describe("ColumnMapperPanel — F-M: columnas ambiguas", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["Precio de venta"] },
      columns_at_risk: [],
    });
  });

  const AMBIGUA = {
    source_column: "Precio de venta",
    normalized_column: "precio_de_venta",
    sample_values: ["1500"],
    target_field: null,
    confidence: 0,
    source: "none" as const,
    status: "ambiguo" as const,
    options: ["amount", "unit_price"],
    duda: "¿es el precio de cada unidad, o el total de la línea?",
  };

  test("muestra la duda del backend, sin reescribirla", async () => {
    mockGetColumnMappings.mockResolvedValue([AMBIGUA]);
    renderPanel();

    await waitFor(() => {
      expect(
        screen.getAllByText("¿es el precio de cada unidad, o el total de la línea?").length,
      ).toBeGreaterThan(0);
    });
  });

  test("entra en la lista de revisión previa al confirm", async () => {
    mockGetColumnMappings.mockResolvedValue([AMBIGUA]);
    renderPanel();

    // Aparece DOS veces —en la tabla y en «Revisá antes de confirmar»— porque una
    // columna ambigua es exactamente sobre la que el backend pide una decisión.
    // Dejarla fuera de esa lista era el bug: se podía confirmar sin verla.
    await waitFor(() => {
      expect(
        screen.getAllByText("¿es el precio de cada unidad, o el total de la línea?"),
      ).toHaveLength(2);
    });
  });

  test("ofrece los candidatos con la etiqueta del catálogo, no el nombre técnico", async () => {
    mockGetColumnMappings.mockResolvedValue([AMBIGUA]);
    renderPanel();

    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: "Monto de venta" }).length).toBeGreaterThan(0);
    });
    expect(
      screen.getAllByRole("button", { name: "Precio unitario vendido" }).length,
    ).toBeGreaterThan(0);
    // Y sólo los que el backend ofreció: la pantalla no agrega candidatos propios.
    expect(screen.queryByRole("button", { name: "Cantidad" })).not.toBeInTheDocument();
  });

  test("elegir un candidato mapea la columna", async () => {
    mockGetColumnMappings.mockResolvedValue([AMBIGUA]);
    renderPanel();

    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: "Monto de venta" }).length).toBeGreaterThan(0);
    });
    const [primerCandidato] = screen.getAllByRole("button", { name: "Monto de venta" });
    fireEvent.click(primerCandidato!);

    await waitFor(() => {
      const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
      expect(selects.some((s) => s.value === "amount")).toBe(true);
    });
  });

  test("un concepto sin campo en esta hoja se explica, y no inventa candidatos", async () => {
    mockGetColumnMappings.mockResolvedValue([
      {
        ...AMBIGUA,
        source_column: "Flete",
        normalized_column: "flete",
        status: "unmapped" as const,
        options: [],
        duda: "Una hoja de ventas no tiene dónde poner un envío.",
      },
    ]);
    renderPanel();

    await waitFor(() => {
      expect(
        screen.getAllByText("Una hoja de ventas no tiene dónde poner un envío.").length,
      ).toBeGreaterThan(0);
    });
    expect(screen.queryByRole("button", { name: "Monto de venta" })).not.toBeInTheDocument();
  });

  test("lo que no se reconoció no muestra ninguna explicación", async () => {
    mockGetColumnMappings.mockResolvedValue([
      {
        ...AMBIGUA,
        source_column: "ColRara99",
        normalized_column: "colrara99",
        status: "unmapped" as const,
        options: [],
        duda: null,
      },
    ]);
    renderPanel();

    await waitFor(() => {
      expect(screen.getAllByText("ColRara99").length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/¿es el precio/)).not.toBeInTheDocument();
  });
});

describe("ColumnMapperPanel — F-B: acciones masivas (camino plano)", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["Fecha", "Precio de venta", "Observaciones libres", "Columna vacía"],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-15"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Precio de venta",
        normalized_column: "precio_de_venta",
        sample_values: ["1500"],
        target_field: null,
        confidence: 0,
        source: "none",
        status: "ambiguo",
        options: ["amount", "unit_price"],
        duda: "¿es el precio de cada unidad, o el total de la línea?",
      },
      {
        source_column: "Observaciones libres",
        normalized_column: "observaciones_libres",
        sample_values: ["Cliente frecuente"],
        target_field: null,
        confidence: 0,
        source: "none",
        status: "unmapped",
      },
      {
        source_column: "Columna vacía",
        normalized_column: "columna_vacia",
        sample_values: ["", "  ", "nan"],
        target_field: null,
        confidence: 0,
        source: "none",
        status: "unmapped",
      },
    ]);
  });

  // El nombre de columna aparece dos veces cuando también entra en "Revisá
  // antes de confirmar" (F-C/F-M): se toma la fila de la TABLA principal, la
  // única envuelta en ".grid".
  function selectDe(columna: string): HTMLSelectElement {
    const fila = screen
      .getAllByText(columna)
      .map((el) => el.closest(".grid"))
      .find((f): f is HTMLElement => f !== null);
    return within(fila as HTMLElement).getByRole("combobox") as HTMLSelectElement;
  }

  test("la barra no se muestra si no hay nada para las tres acciones", async () => {
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-15"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
    renderPanel();

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("transaction_date");
    });
    expect(screen.queryByText(/Acciones masivas/)).not.toBeInTheDocument();
  });

  test("«Aceptar sugerencias ambiguas» toma el primer candidato y marca la columna tocada", async () => {
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Aceptar sugerencias ambiguas \(1\)/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Aceptar sugerencias ambiguas \(1\)/));

    await waitFor(() => {
      expect(selectDe("Precio de venta").value).toBe("amount");
    });
    // Marcada como tocada: sobrevive un cambio de sección/propósito después
    // (comportamiento ya cubierto por otro describe; acá sólo se confirma el valor).
  });

  test("«Guardar sin mapear como campos propios» no toca la vacía (le compite «ignorar»)", async () => {
    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/Guardar sin mapear como campos propios \(1\)/),
      ).toBeInTheDocument();
    });
    // Sólo "Observaciones libres" tiene datos reales — "Columna vacía" es
    // candidata de "ignorar", no de "campo propio" (mutuamente excluyentes).
    fireEvent.click(screen.getByText(/Guardar sin mapear como campos propios \(1\)/));

    await waitFor(() => {
      expect(selectDe("Observaciones libres").value).toBe(
        "custom_field:observaciones_libres",
      );
    });
    expect(selectDe("Columna vacía").value).toBe("");
  });

  test("«Ignorar columnas vacías» sólo actúa sobre la que no tiene ningún dato real", async () => {
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Ignorar columnas vacías \(1\)/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Ignorar columnas vacías \(1\)/));

    await waitFor(() => {
      expect(selectDe("Columna vacía").value).toBe("ignore");
    });
    expect(selectDe("Observaciones libres").value).toBe("");
  });

  test("no pisa una columna que el usuario ya mapeó a mano antes de usar la acción masiva", async () => {
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/Ignorar columnas vacías \(1\)/)).toBeInTheDocument();
    });
    // El usuario decide a mano que "Columna vacía" en realidad va a notas.
    fireEvent.change(selectDe("Columna vacía"), { target: { value: "notes" } });

    await waitFor(() => {
      // La barra se recalcula: ya no hay nada que ignorar.
      expect(screen.queryByText(/Ignorar columnas vacías/)).not.toBeInTheDocument();
    });
    expect(selectDe("Columna vacía").value).toBe("notes");
  });
});

/**
 * Un archivo de UNA SOLA TABLA no puede traer costos de compra.
 *
 * El importador plano no cobra el envío ni aplica las decisiones de costo —el
 * cobro vive en un closure del camino multi-hoja y la decisión se busca bajo
 * otra clave—, así que el confirm rechaza el archivo con 422 en cuanto ve una
 * columna de envío mapeada o una decisión declarada.
 *
 * Antes de ese guard el import lo ACEPTABA y las ignoraba en silencio: la
 * compra quedaba con un costo más bajo que el real y el margen inflado. La
 * pantalla no puede seguir ofreciendo los tres ejes acá —cada decisión termina
 * en un rechazo— así que nombra el problema y da las dos salidas.
 */
describe("ColumnMapperPanel — tabla única: los costos de compra no se ofrecen", () => {
  function preview(headers: string[]) {
    return {
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "gastos", headers },
      columns_at_risk: [],
    };
  }

  function sugerencia(source_column: string, target_field: string) {
    return {
      source_column,
      normalized_column: source_column.toLowerCase(),
      sample_values: ["1000"],
      target_field,
      confidence: 0.9,
      source: "heuristic",
      status: "mapped",
    };
  }

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
  });

  test("con una columna de envío se explica el rechazo y no se deja confirmar", async () => {
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Monto", "Envio"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "expense_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Envio", "shipping_cost"),
    ]);
    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/todavía no sabe cobrar ni repartir el envío/i),
      ).toBeInTheDocument();
    });
    // Las dos salidas que ofrece el 422, dichas ANTES de apretar.
    expect(screen.getByText(/libro con hojas separadas/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Confirmar/i })).toBeDisabled();
    // Y ningún eje de costo: cada decisión que tomara terminaría en un rechazo.
    expect(screen.queryByText(/cobra una sola vez/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/viene asignado a cada línea/i)).not.toBeInTheDocument();
  });

  test("el flete por línea también lo dispara, y se nombra la columna", async () => {
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Monto", "Flete linea"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "expense_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Flete linea", "shipping_cost_line"),
    ]);
    renderPanel();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Confirmar/i })).toBeDisabled();
    });
    // Nombrar la columna es lo que vuelve accionable la salida «sacala del
    // mapeo»: sin eso hay que adivinar cuál de todas es.
    expect(screen.getAllByText("Flete linea").length).toBeGreaterThan(0);
  });

  test("el mismo archivo SIN columnas de envío importa igual", async () => {
    // El rechazo alcanza a los costos, no al formato. Si esto fuera rojo, el
    // guard estaría bloqueando archivos que el backend acepta.
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Monto", "Descuento"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "expense_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Descuento", "discount"),
    ]);
    renderPanel();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Confirmar/i })).toBeEnabled();
    });
    expect(
      screen.queryByText(/todavía no sabe cobrar ni repartir el envío/i),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // Nunca una decisión de costo por este camino: el importador plano no la
    // aplica y el confirm rechaza el archivo apenas la ve.
    expect(mockConfirmFile.mock.calls[0]?.[9]).toBeUndefined();
  });

  test("tampoco se le pide el reparto al servidor para un archivo que no puede tenerlo", async () => {
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Monto", "Envio"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "expense_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Envio", "shipping_cost"),
    ]);
    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/todavía no sabe cobrar ni repartir el envío/i),
      ).toBeInTheDocument();
    });
    expect(mockPurchaseGroups).not.toHaveBeenCalled();
  });
});

describe("ColumnMapperPanel — F-H6.c multi-hoja: sólo viaja lo que el usuario cambió", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "mixed",
        mapping_contexts: [
          {
            context_id: "hoja1",
            label: "Compras",
            source_kind: "sheet",
            entity_type: "expense",
            headers: ["Fecha", "Monto", "Descuento"],
            fields: null,
            preview_rows: [{ Fecha: "2024-03-05", Monto: "12000", Descuento: "2000" }],
            row_count: 1,
          },
        ],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-05"],
        target_field: "expense_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["12000"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Descuento",
        normalized_column: "descuento",
        sample_values: ["2000"],
        target_field: "discount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
    ]);
  });

  test("una hoja en su default no viaja en el payload", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/ya incluye el descuento/i)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // Mandar los defaults sería ruido: el backend ya los aplica, y omitir la hoja
    // deja escrito en la traza que el usuario no decidió nada sobre su costo.
    expect(mockConfirmFile.mock.calls[0]?.[9]).toEqual([]);
  });

  test("y sí viaja apenas se aparta de un default", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /El monto es el bruto/ })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("radio", { name: /El monto es el bruto/ }));
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    expect(mockConfirmFile.mock.calls[0]?.[9]).toEqual([
      {
        context_id: "hoja1",
        base: "monto_sin_ajustes",
        shared_shipping: "no_distribuir",
        line_shipping: "gasto_aparte",
      },
    ]);
  });

  test("volver al default después de haber elegido tampoco manda la hoja", async () => {
    /**
     * El caso que el filtro existe para cubrir. Una hoja que nunca se tocó no
     * tiene entrada en el estado y no viaja por eso; una que se cambió y se
     * volvió atrás SÍ la tiene, y sin el filtro viajaría declarando un default
     * que el backend ya aplica solo. Arrepentirse tiene que dejar el archivo como
     * si nunca se hubiera tocado.
     */
    renderPanel();
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /El monto es el bruto/ })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("radio", { name: /El monto es el bruto/ }));
    fireEvent.click(screen.getByRole("radio", { name: /El monto ya es el final/ }));
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    expect(mockConfirmFile.mock.calls[0]?.[9]).toEqual([]);
  });
});

/**
 * F-H6.d — el tercer eje del costo de compra, punta a punta desde la pantalla.
 *
 * El backend sabía repartir el envío de un comprobante entre sus líneas y el
 * tipo del servicio ya tenía `shared_shipping`, pero el panel lo descartaba al
 * armar el payload: la distribución era INALCANZABLE desde la app. Es el mismo
 * agujero que F-H3.e, en otra fase.
 */
describe("ColumnMapperPanel — F-H6.d: el envío compartido llega al confirm", () => {
  const GRUPOS_REPARTIBLES = [
    {
      context_id: "hoja1",
      label: "Compras",
      puede_distribuir: true,
      motivo: null,
      grupos_total: 1,
      grupos: [
        {
          comprobante: "A-0001",
          proveedor: "Distribuidora Sur",
          subtotal: "10000.00",
          envio_compartido: "500.00",
          repartido: "500.00",
          sin_repartir: "0.00",
          distribuible: true,
          motivo_no_distribuible: null,
          lineas: [
            {
              row_index: 0,
              producto: "Yerba",
              subtotal: "5000.00",
              envio_asignado: "250.00",
              costo_total: "5250.00",
              costo_unitario_final: "525.00",
            },
            {
              row_index: 1,
              producto: "Azúcar",
              subtotal: "3000.00",
              envio_asignado: "150.00",
              costo_total: "3150.00",
              costo_unitario_final: "315.00",
            },
            {
              row_index: 2,
              producto: "Fideos",
              subtotal: "2000.00",
              envio_asignado: "100.00",
              costo_total: "2100.00",
              costo_unitario_final: "210.00",
            },
          ],
        },
      ],
      filas_sin_comprobante: 0,
    },
  ];

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue(GRUPOS_REPARTIBLES);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "mixed",
        mapping_contexts: [
          {
            context_id: "hoja1",
            label: "Compras",
            source_kind: "sheet",
            entity_type: "expense",
            headers: ["Fecha", "Monto", "Comprobante", "Envio"],
            fields: null,
            preview_rows: [],
            row_count: 3,
          },
        ],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-05"],
        target_field: "expense_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["5000"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Comprobante",
        normalized_column: "comprobante",
        sample_values: ["A-0001"],
        target_field: "invoice_number",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Envio",
        normalized_column: "envio",
        sample_values: ["500"],
        target_field: "shipping_cost",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
    ]);
  });

  test("el eje aparece porque la hoja mapea el envío y el servidor puede repartirlo", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/cobra una sola vez/i)).toBeInTheDocument();
    });
  });

  test("no aparece si el servidor dice que esta hoja no se puede repartir", async () => {
    // La columna está mapeada igual: la condición que falta es la del servidor.
    // Ofrecer el reparto acá dejaría al usuario eligiendo algo que no va a pasar.
    mockPurchaseGroups.mockResolvedValue([
      {
        ...GRUPOS_REPARTIBLES[0],
        puede_distribuir: false,
        motivo: "sin_identidad_de_comprobante",
        grupos: [],
        grupos_total: 0,
      },
    ]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/no se puede repartir/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/cobra una sola vez/i)).not.toBeInTheDocument();
  });

  test("elegir repartir hace que shared_shipping VIAJE en el confirm", async () => {
    renderPanel();
    await waitFor(() => {
      expect(
        screen.getByRole("radio", { name: /Se reparte entre los productos/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("radio", { name: /Se reparte entre los productos/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    expect(mockConfirmFile.mock.calls[0]?.[9]).toEqual([
      {
        context_id: "hoja1",
        base: "monto_incluye",
        shared_shipping: "por_subtotal",
        line_shipping: "gasto_aparte",
      },
    ]);
  });

  test("el reparto que se muestra es el que devolvió el servidor", async () => {
    // Los importes salen tal cual de la respuesta: si el frontend los
    // recalculara podría mostrar una división distinta de la que se persiste.
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/A-0001/)).toBeInTheDocument();
    });
    expect(screen.getByText(/se reparten \$250 \/ \$150 \/ \$100/)).toBeInTheDocument();
  });
});

/**
 * F-H6.d — el preview del reparto tiene que ver lo MISMO que va a ver el import.
 *
 * La decisión de envío sin comprobante (F-H6.b) cambia si una hoja puede formar
 * un grupo: sin mandarla, una hoja donde el usuario ya declaró «toda la hoja es
 * una compra» se previsualizaría como no repartible, contradiciendo lo que
 * acaba de elegir en la misma pantalla.
 */
describe("ColumnMapperPanel — F-H6.d: el preview ve la decisión de envío", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "mixed",
        mapping_contexts: [
          {
            context_id: "hoja1",
            label: "Compras",
            source_kind: "sheet",
            entity_type: "expense",
            // Sin columna de comprobante: es el caso donde F-H6.b pregunta.
            headers: ["Fecha", "Monto", "Envio"],
            fields: null,
            preview_rows: [],
            row_count: 3,
          },
        ],
      },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2024-03-05"],
        target_field: "expense_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["5000"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
      {
        source_column: "Envio",
        normalized_column: "envio",
        sample_values: ["500"],
        target_field: "shipping_cost",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
        context_id: "hoja1",
      },
    ]);
  });

  test("un tenant sin el motor de costos deja de preguntar en vez de comerse un 403 por tecla", async () => {
    /**
     * `/purchase-groups` responde 403 mientras el motor de costos está detrás de
     * la compuerta de rollout. Eso NO es un error que mostrar: la degradación
     * correcta es que el tercer eje y la vista previa no aparezcan. Lo que sí
     * hay que evitar es reintentar: la clave de la consulta cambia con cada
     * edición del mapeo, así que sin freno es un 403 por cada cambio.
     */
    mockPurchaseGroups.mockRejectedValue({ response: { status: 403 } });
    renderPanel();

    await waitFor(() => expect(mockPurchaseGroups).toHaveBeenCalled());
    const llamadasTrasEl403 = mockPurchaseGroups.mock.calls.length;

    // Se cambia el mapeo: sin el freno, esto dispara otra consulta.
    const select = await waitFor(() => {
      const el = document.querySelector<HTMLSelectElement>(
        'select[data-suggests="shipping_cost"]',
      );
      expect(el).not.toBeNull();
      return el!;
    });
    fireEvent.change(select, { target: { value: "ignore" } });
    fireEvent.change(select, { target: { value: "shipping_cost" } });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Es un solo envío/ })).toBeInTheDocument();
    });
    expect(mockPurchaseGroups).toHaveBeenCalledTimes(llamadasTrasEl403);
    expect(screen.queryByText(/cobra una sola vez/i)).not.toBeInTheDocument();
  });

  test("declarar «es un solo envío» se le pregunta también al preview", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Es un solo envío/ })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /Es un solo envío/ }));

    await waitFor(() => {
      const ultima = mockPurchaseGroups.mock.calls.at(-1);
      expect(ultima?.[1]?.shippingDecisions).toEqual([
        { context_id: "hoja1", action: "una_por_hoja" },
      ]);
    });
  });
});

/**
 * F-C — el banner de faltantes explica y lleva al selector.
 *
 * Decía «falta un dato obligatorio. Revisá la hoja más arriba.»: no decía CUÁL,
 * no decía POR QUÉ, y no llevaba a ningún lado. La persona quedaba con el botón
 * apagado y sin una acción concreta — que es la misma queja que originó F-C del
 * lado del 422.
 */
describe("ColumnMapperPanel — F-C: el banner de faltantes nombra, explica y lleva", () => {
  function preview(headers: string[]) {
    return {
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: {
        inferred_type: "mixed",
        mapping_contexts: [
          {
            context_id: "hoja1",
            label: "Ventas marzo",
            source_kind: "sheet",
            entity_type: "sale",
            headers,
            fields: null,
            preview_rows: [],
            row_count: 2,
          },
        ],
      },
      columns_at_risk: [],
    };
  }

  function sugerencia(source_column: string, target_field: string | null) {
    return {
      source_column,
      normalized_column: source_column.toLowerCase(),
      sample_values: ["x"],
      target_field,
      confidence: 0.9,
      source: target_field ? "heuristic" : "none",
      status: target_field ? "mapped" : "unmapped",
      context_id: "hoja1",
    };
  }

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
  });

  test("nombra el campo con su etiqueta y dice qué se pierde si no lo mapea", async () => {
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Monto"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "transaction_date"),
      sugerencia("Monto", "amount"),
    ]);
    renderPanel();

    // Se desmapea la fecha a propósito: es la forma exacta del incidente ASTERIA
    // (la columna existe, el usuario la manda a otro lado, el requerido queda
    // descubierto y el confirm devuelve 422 sin decir cómo salir).
    const selectFecha = await waitFor(() => {
      const el = document.querySelector<HTMLSelectElement>(
        'select[data-suggests="transaction_date"]',
      );
      expect(el).not.toBeNull();
      return el!;
    });
    fireEvent.change(selectFecha, { target: { value: "ignore" } });

    await waitFor(() => {
      expect(screen.getAllByText("Fecha de venta").length).toBeGreaterThan(0);
    });
    expect(screen.getByText(/nunca se le pone la de hoy/)).toBeInTheDocument();
    // El nombre técnico nunca se le muestra a la persona.
    expect(screen.queryByText(/transaction_date/)).not.toBeInTheDocument();
  });

  test("clickear el faltante enfoca el select de la columna que lo traía", async () => {
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Monto"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "transaction_date"),
      sugerencia("Monto", "amount"),
    ]);
    renderPanel();

    const selectFecha = await waitFor(() => {
      const el = document.querySelector<HTMLSelectElement>(
        'select[data-suggests="transaction_date"]',
      );
      expect(el).not.toBeNull();
      return el!;
    });
    fireEvent.change(selectFecha, { target: { value: "ignore" } });

    const enlace = await screen.findByRole("button", { name: /Fecha de venta — ir a la hoja/ });
    fireEvent.click(enlace);
    // El salto va al DESTINO: la única columna cuya sugerencia apuntaba a ese
    // campo. Enfocar cualquier otra sería elegir al azar.
    expect(document.activeElement).toBe(selectFecha);
  });

  test("sin una columna que apunte al campo, resalta la hoja en vez de elegir al azar", async () => {
    // Ninguna sugerencia apunta a `transaction_date`: no hay a qué columna
    // mandar el foco, así que se señala la hoja y decide la persona.
    mockGetPreview.mockResolvedValue(preview(["Monto", "Comentario"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Monto", "amount"),
      sugerencia("Comentario", "notes"),
    ]);
    const { container } = renderPanel();

    const enlace = await screen.findByRole("button", { name: /Fecha de venta — ir a la hoja/ });
    fireEvent.click(enlace);

    await waitFor(() => {
      const tarjeta = container.querySelector('[data-sheet-card="hoja1"]');
      expect(tarjeta?.className).toContain("ring-2");
    });
  });

  test("con DOS columnas candidatas tampoco elige: resalta la hoja", async () => {
    /**
     * El caso que la regla existe para cubrir, y el que un `candidatos[0]` deja
     * pasar. Dos columnas sugerían la fecha y la persona desmapeó las dos:
     * enfocar la primera del orden del archivo es exactamente el defecto que
     * arregló la corrección V10 —quedarse con la primera y decidir por el
     * usuario— sólo que en el foco en vez de en el mapeo.
     */
    mockGetPreview.mockResolvedValue(preview(["Fecha", "Fecha alta", "Monto"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Fecha", "transaction_date"),
      sugerencia("Fecha alta", "transaction_date"),
      sugerencia("Monto", "amount"),
    ]);
    const { container } = renderPanel();

    const candidatos = await waitFor(() => {
      const els = Array.from(
        document.querySelectorAll<HTMLSelectElement>(
          'select[data-suggests="transaction_date"]',
        ),
      );
      expect(els).toHaveLength(2);
      return els;
    });
    for (const el of candidatos) fireEvent.change(el, { target: { value: "ignore" } });

    const enlace = await screen.findByRole("button", {
      name: /Fecha de venta — ir a la hoja/,
    });
    fireEvent.click(enlace);

    await waitFor(() => {
      const tarjeta = container.querySelector('[data-sheet-card="hoja1"]');
      expect(tarjeta?.className).toContain("ring-2");
    });
    expect(candidatos).not.toContain(document.activeElement);
  });

  test("una hoja con las dos cosas rotas nombra las dos", async () => {
    // Antes el mensaje era un o-lo-uno-o-lo-otro: con un requerido descubierto,
    // la colisión no se nombraba y la hoja seguía bloqueada por algo invisible.
    mockGetPreview.mockResolvedValue(preview(["Monto", "Importe", "Comentario"]));
    mockGetColumnMappings.mockResolvedValue([
      sugerencia("Monto", "amount"),
      sugerencia("Importe", "amount"),
      sugerencia("Comentario", "notes"),
    ]);
    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/falta un dato obligatorio y hay dos columnas para el mismo campo/),
      ).toBeInTheDocument();
    });
  });
});

/**
 * F-A — cambiar de sección no puede borrar lo que la persona ya mapeó.
 *
 * La inicialización del mapeo era un REEMPLAZO: al reasignar la hoja (Ventas →
 * Gastos) el estado entero se pisaba con las sugerencias de la entidad nueva, y
 * veinte columnas mapeadas a mano se perdían por corregir la sección.
 *
 * Ahora es un merge con una condición que no es obvia: lo tocado se conserva
 * SÓLO si sigue siendo elegible en la entidad nueva. Preservar a ciegas un
 * canónico que allá no existe reintroduce el «(campo desconocido)» que cerró el
 * catálogo de fuente única.
 */
describe("ColumnMapperPanel — F-A: cambiar de sección conserva lo mapeado a mano", () => {
  const PREVIEW = {
    file_id: "file-1",
    processing_status: "NEEDS_CONFIRMATION",
    parsed_summary_json: {
      inferred_type: "mixed",
      mapping_contexts: [
        {
          context_id: "hoja1",
          label: "Movimientos",
          source_kind: "sheet",
          entity_type: "sale",
          headers: ["Fecha", "Monto", "Detalle"],
          fields: null,
          preview_rows: [],
          row_count: 12,
        },
      ],
    },
    columns_at_risk: [],
  };

  function sugerencia(source_column: string, target_field: string | null) {
    return {
      source_column,
      normalized_column: source_column.toLowerCase(),
      sample_values: ["x"],
      target_field,
      confidence: 0.9,
      source: target_field ? "heuristic" : "none",
      status: target_field ? "mapped" : "unmapped",
      context_id: "hoja1",
    };
  }

  // Las sugerencias de cada sección salen de schemas distintos: la misma columna
  // «Fecha» es `transaction_date` en Ventas y `expense_date` en Gastos. Eso es
  // justo lo que SÍ tiene que recalcularse al cambiar de sección.
  const POR_ENTIDAD: Record<string, ReturnType<typeof sugerencia>[]> = {
    sale: [
      sugerencia("Fecha", "transaction_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Detalle", "notes"),
    ],
    expense: [
      sugerencia("Fecha", "expense_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Detalle", "category"),
    ],
  };

  /** El `<select>` de una columna, ubicado por el nombre de la columna. */
  function selectDe(columna: string): HTMLSelectElement {
    const fila = screen.getByTitle(columna).closest(".grid");
    return within(fila as HTMLElement).getByRole("combobox") as HTMLSelectElement;
  }

  function cambiarSeccion(a: string) {
    fireEvent.change(screen.getByLabelText("Sección de la hoja Movimientos"), {
      target: { value: a },
    });
  }

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue(PREVIEW);
    mockGetColumnMappings.mockImplementation((_fileId: string, entity: string) =>
      Promise.resolve(POR_ENTIDAD[entity] ?? []),
    );
  });

  test("lo elegido a mano sobrevive; lo no tocado se recalcula", async () => {
    renderPanel();

    await waitFor(() => {
      expect(selectDe("Detalle").value).toBe("notes");
    });
    // `payment_method` existe en Ventas y en Gastos: sigue siendo elegible.
    fireEvent.change(selectDe("Detalle"), { target: { value: "payment_method" } });

    cambiarSeccion("expense");

    await waitFor(() => {
      // La columna que nadie tocó adopta la sugerencia del schema nuevo.
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    // Y la que la persona eligió a mano queda como la dejó.
    expect(selectDe("Detalle").value).toBe("payment_method");
  });

  test("un target que la sección nueva no tiene NO se conserva", async () => {
    renderPanel();

    await waitFor(() => {
      expect(selectDe("Detalle").value).toBe("notes");
    });
    // `product_name` sólo existe en Ventas.
    fireEvent.change(selectDe("Detalle"), { target: { value: "product_name" } });

    cambiarSeccion("expense");

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    // Conservarlo dejaría el select en «(campo desconocido)»: cae a la
    // sugerencia de Gastos, sin inventar ningún reemplazo.
    expect(selectDe("Detalle").value).not.toBe("product_name");
    expect(selectDe("Detalle").value).toBe("category");
  });

  test("un campo personalizado sobrevive: no pertenece a ningún schema", async () => {
    renderPanel();

    await waitFor(() => {
      expect(selectDe("Detalle").value).toBe("notes");
    });
    fireEvent.change(selectDe("Detalle"), { target: { value: "__custom__" } });
    const entrada = screen.getByPlaceholderText("nombre_del_campo");
    fireEvent.change(entrada, { target: { value: "obs libres" } });
    fireEvent.click(screen.getByRole("button", { name: "OK" }));

    await waitFor(() => {
      expect(selectDe("Detalle").value).toBe("custom_field:obs_libres");
    });

    cambiarSeccion("expense");

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    expect(selectDe("Detalle").value).toBe("custom_field:obs_libres");
  });

  test("sin tocar nada, todo se recalcula con el schema nuevo", async () => {
    renderPanel();

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("transaction_date");
    });

    cambiarSeccion("expense");

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    expect(selectDe("Detalle").value).toBe("category");
    expect(selectDe("Monto").value).toBe("amount");
  });
});

/**
 * F-A — mismo mecanismo que "cambiar de sección" arriba, pero del lado del
 * camino PLANO (archivo ambiguo, un único propósito exclusivo en vez de una
 * hoja por sección). Antes `choosePurpose` vaciaba `mappings` y limpiaba
 * `touchedRef` a ciegas — cambiar el propósito de un archivo con 20 columnas
 * ya corregidas a mano las perdía todas.
 */
describe("ColumnMapperPanel — F-A: cambiar de propósito (camino plano) conserva lo mapeado a mano", () => {
  const PREVIEW_AMBIGUO = {
    file_id: "file-1",
    processing_status: "NEEDS_CONFIRMATION",
    parsed_summary_json: {
      inferred_type: "general",
      headers: ["Fecha", "Monto", "Detalle"],
    },
    columns_at_risk: [],
  };

  function sugerencia(source_column: string, target_field: string | null) {
    return {
      source_column,
      normalized_column: source_column.toLowerCase(),
      sample_values: ["x"],
      target_field,
      confidence: 0.9,
      source: target_field ? "heuristic" : "none",
      status: target_field ? "mapped" : "unmapped",
    };
  }

  const POR_ENTIDAD: Record<string, ReturnType<typeof sugerencia>[]> = {
    sale: [
      sugerencia("Fecha", "transaction_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Detalle", "notes"),
    ],
    expense: [
      sugerencia("Fecha", "expense_date"),
      sugerencia("Monto", "amount"),
      sugerencia("Detalle", "category"),
    ],
  };

  // El camino plano no pone `title` en el nombre de columna (a diferencia de
  // `SheetMapperSection`) — el nombre vive en un `<span>` de texto plano.
  function selectDe(columna: string): HTMLSelectElement {
    const fila = screen.getByText(columna).closest(".grid");
    return within(fila as HTMLElement).getByRole("combobox") as HTMLSelectElement;
  }

  // El selector de propósito solo aparece después de que cargue el preview
  // (isAmbiguous depende de `parsed_summary_json.inferred_type`) — a
  // diferencia de `cambiarSeccion` (multi-hoja), acá hace falta esperarlo.
  async function cambiarProposito(nombre: string) {
    const boton = await screen.findByRole("button", { name: nombre });
    fireEvent.click(boton);
  }

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue(PREVIEW_AMBIGUO);
    mockGetColumnMappings.mockImplementation((_fileId: string, entity: string) =>
      Promise.resolve(POR_ENTIDAD[entity] ?? []),
    );
  });

  test("lo elegido a mano sobrevive; lo no tocado se recalcula", async () => {
    renderPanel();
    await cambiarProposito("Ventas");

    await waitFor(() => {
      expect(selectDe("Detalle").value).toBe("notes");
    });
    // `payment_method` existe en Ventas y en Gastos: sigue siendo elegible.
    fireEvent.change(selectDe("Detalle"), { target: { value: "payment_method" } });

    await cambiarProposito("Gastos");

    await waitFor(() => {
      // La columna que nadie tocó adopta la sugerencia del propósito nuevo.
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    // Y la que la persona eligió a mano queda como la dejó.
    expect(selectDe("Detalle").value).toBe("payment_method");
  });

  test("un target que el propósito nuevo no tiene NO se conserva", async () => {
    renderPanel();
    await cambiarProposito("Ventas");

    await waitFor(() => {
      expect(selectDe("Detalle").value).toBe("notes");
    });
    // `customer_name` sólo existe en Ventas.
    fireEvent.change(selectDe("Detalle"), { target: { value: "customer_name" } });

    await cambiarProposito("Gastos");

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    // Conservarlo dejaría el select en «(campo desconocido)»: cae a la
    // sugerencia de Gastos, sin inventar ningún reemplazo.
    expect(selectDe("Detalle").value).not.toBe("customer_name");
    expect(selectDe("Detalle").value).toBe("category");
  });

  test("sin tocar nada, todo se recalcula con el propósito nuevo", async () => {
    renderPanel();
    await cambiarProposito("Ventas");

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("transaction_date");
    });

    await cambiarProposito("Gastos");

    await waitFor(() => {
      expect(selectDe("Fecha").value).toBe("expense_date");
    });
    expect(selectDe("Detalle").value).toBe("category");
    expect(selectDe("Monto").value).toBe("amount");
  });
});

/**
 * F-B.1 — la procedencia de un mapeo se cuenta en castellano, sin porcentaje.
 *
 * El «Heurística · 75%» de abajo de cada columna fue lo primero que el usuario
 * reportó no entender, y con razón: el 75% está hardcodeado para TODO lo que
 * resuelve la heurística (`column_mapping_service.py`) y el fuzzy escala su
 * ratio a un techo de 65%, así que ningún fuzzy podía superar nunca a ningún
 * heurístico. El número no era la probabilidad calibrada de nada.
 */
describe("ColumnMapperPanel — F-B.1: la procedencia se dice en castellano", () => {
  const PREVIEW = {
    file_id: "file-1",
    processing_status: "NEEDS_CONFIRMATION",
    parsed_summary_json: { inferred_type: "ventas", headers: ["Monto"] },
    columns_at_risk: [],
  };

  function sugerencia(
    source: string,
    target_field: string | null,
    confidence = 0.75,
  ) {
    return {
      source_column: "Monto",
      normalized_column: "monto",
      sample_values: ["1500"],
      target_field,
      confidence,
      source,
      status: target_field ? "mapped" : "unmapped",
    };
  }

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetPreview.mockResolvedValue(PREVIEW);
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
  });

  test("no queda NINGÚN porcentaje visible en el panel", async () => {
    // Es el criterio de aceptación de F-B.1 y lo que evita que el número
    // vuelva por descuido: cualquier reintroducción del `%` rompe acá.
    mockGetPreview.mockResolvedValue({
      ...PREVIEW,
      parsed_summary_json: {
        inferred_type: "ventas",
        headers: ["Monto", "Fecha", "Detalle", "ColX"],
      },
    });
    mockGetColumnMappings.mockResolvedValue([
      { ...sugerencia("heuristic", "amount"), source_column: "Monto" },
      {
        ...sugerencia("tenant_history", "transaction_date", 0.95),
        source_column: "Fecha",
        normalized_column: "fecha",
      },
      {
        ...sugerencia("fuzzy", "notes", 0.46),
        source_column: "Detalle",
        normalized_column: "detalle",
      },
      {
        ...sugerencia("llm", "payment_method", 0.8),
        source_column: "ColX",
        normalized_column: "colx",
      },
    ]);

    const { container } = renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText("Sugerido por el nombre de la columna"),
      ).toBeInTheDocument();
    });
    expect(container.textContent ?? "").not.toMatch(/\d+\s*%/);
  });

  test.each([
    ["tenant_history", "Usado antes por tu negocio"],
    ["heuristic", "Sugerido por el nombre de la columna"],
    // «nombre parecido» y no «los valores»: `_fuzzy_match` compara el NOMBRE
    // normalizado del encabezado contra los keywords, nunca las celdas.
    ["fuzzy", "Sugerido por un nombre parecido"],
    ["llm", "Sugerido por Véktor"],
  ])("source %s → «%s»", async (source, frase) => {
    mockGetColumnMappings.mockResolvedValue([sugerencia(source, "amount")]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(frase)).toBeInTheDocument();
    });
  });

  test("elegir otro destino dice «Lo elegiste vos», y volver al sugerido NO", async () => {
    // El caso que obliga a comparar valores en vez de llevar un flag `touched`:
    // si alguien prueba otro campo y termina dejando el que propuso Véktor, ese
    // dato no salió de esa persona y la pantalla no puede decir que sí.
    mockGetColumnMappings.mockResolvedValue([sugerencia("heuristic", "amount")]);

    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText("Sugerido por el nombre de la columna"),
      ).toBeInTheDocument();
    });

    const select = screen.getAllByRole("combobox")[0]!;
    fireEvent.change(select, { target: { value: "quantity" } });
    await waitFor(() => {
      expect(screen.getByText("Lo elegiste vos")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Sugerido por el nombre de la columna"),
    ).not.toBeInTheDocument();

    fireEvent.change(select, { target: { value: "amount" } });
    await waitFor(() => {
      expect(
        screen.getByText("Sugerido por el nombre de la columna"),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("Lo elegiste vos")).not.toBeInTheDocument();
  });

  test("mandar la columna a «Ignorar» no cuenta ninguna procedencia", async () => {
    mockGetColumnMappings.mockResolvedValue([sugerencia("heuristic", "amount")]);

    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText("Sugerido por el nombre de la columna"),
      ).toBeInTheDocument();
    });

    fireEvent.change(screen.getAllByRole("combobox")[0]!, {
      target: { value: "ignore" },
    });

    await waitFor(() => {
      expect(
        screen.queryByText("Sugerido por el nombre de la columna"),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByText("Lo elegiste vos")).not.toBeInTheDocument();
  });

  test("una columna sin mapear y sin procedencia no renderiza nada", async () => {
    mockGetColumnMappings.mockResolvedValue([sugerencia("none", null, 0)]);

    renderPanel();

    // La columna se dibuja; lo que no aparece es la línea de procedencia.
    await waitFor(() =>
      expect(screen.getAllByText("Monto").length).toBeGreaterThan(0),
    );
    for (const frase of [
      "Lo elegiste vos",
      "Usado antes por tu negocio",
      "Sugerido por el nombre de la columna",
      "Sugerido por un nombre parecido",
      "Sugerido por Véktor",
    ]) {
      expect(screen.queryByText(frase)).not.toBeInTheDocument();
    }
  });
});

describe("ColumnMapperPanel — B.1: el modal de columnas sin mapear usa el selector único", () => {
  // Este modal era el CUARTO lugar que dibujaba un `<select>` de destino, y el
  // único sin tests: los otros tres ya se habían unificado en `TargetSelect`.
  // Sin estas pruebas, "los tests pasan sin tocarse" no dice nada sobre él.
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["Fecha", "Monto", "Sucursal"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2026-03-01"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1000"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      // La que dispara el modal: sin destino.
      {
        source_column: "Sucursal",
        normalized_column: "sucursal",
        sample_values: ["Centro"],
        target_field: null,
        confidence: 0,
        source: "none",
        status: "unmapped",
      },
    ]);
  });

  async function abrirModal() {
    renderPanel();
    // Esperar a que las sugerencias estén EN PANTALLA, no sólo a que el botón
    // exista: con la lista todavía vacía no hay columnas sin mapear, el botón
    // ya está habilitado y el click confirma de una sin abrir el modal.
    await screen.findByText("Confirmar (1 sin mapear)");
    fireEvent.click(screen.getByRole("button", { name: /Confirmar \(1 sin mapear\)/i }));
    // Acotado al modal: el panel de atrás sigue montado con sus propios selects.
    const titulo = await screen.findByText("Columna sin mapear");
    return titulo.closest("div.fixed") as HTMLElement;
  }

  test("ofrece los campos del catálogo bajo «Elegir campo...»", async () => {
    const modal = await abrirModal();
    expect(within(modal).getByText("Sucursal")).toBeInTheDocument();

    const select = within(modal).getByRole("combobox") as HTMLSelectElement;
    const opciones = Array.from(select.options).map((o) => o.textContent);
    expect(opciones[0]).toBe("Elegir campo...");
    expect(opciones).toEqual(expect.arrayContaining(["Monto de venta", "Fecha de venta"]));
  });

  test("no duplica adentro del select las acciones que ya son botones propios", async () => {
    // «Ignorar esta columna» y «Guardar como campo personalizado» viven como
    // botones del modal. `TargetSelect` las ofrece por default, así que
    // unificar sin apagarlas le habría agregado dos opciones que nunca tuvo.
    const modal = await abrirModal();
    const select = within(modal).getByRole("combobox") as HTMLSelectElement;
    const valores = Array.from(select.options).map((o) => o.value);
    expect(valores).not.toContain("ignore");
    expect(valores).not.toContain("__custom__");
    expect(Array.from(select.options).map((o) => o.textContent)).not.toContain("Sin mapear");

    // Y siguen estando como botones, que es de donde no se movieron.
    expect(
      within(modal).getByRole("button", { name: /Ignorar esta columna/i }),
    ).toBeInTheDocument();
    expect(
      within(modal).getByRole("button", { name: /Guardar como campo personalizado/i }),
    ).toBeInTheDocument();
  });

  test("elegir un campo lo manda en el confirm", async () => {
    const modal = await abrirModal();
    const select = within(modal).getByRole("combobox") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "notes" } });
    fireEvent.click(within(modal).getByRole("button", { name: /^Confirmar$/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // `confirmFile(fileId, confirmedFields, columnMappings, …)` — posicional.
    const enviado = mockConfirmFile.mock.calls[0]![2] as ColumnMapping[];
    expect(enviado).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ source_column: "Sucursal", target_field: "notes" }),
      ]),
    );
  });
});

describe("ColumnMapperPanel — F-A: la etiqueta del campo propio llega al confirm", () => {
  // El recorrido del label es sugerencia → estado → payload → backend. Los
  // tests del backend cubren las dos puntas; este cubre el tramo del medio, que
  // es el que el plan rector no contemplaba: sin él, el confirm derivaba la
  // etiqueta de `source_column` y coincidían sólo por casualidad.
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetFieldCatalog.mockResolvedValue(FIELD_CATALOG);
    mockRecomputeColumnRisk.mockResolvedValue([]);
    mockInventoryEffects.mockResolvedValue([]);
    mockPurchaseGroups.mockResolvedValue([]);
    mockConfirmFile.mockResolvedValue({ file_id: "file-1", status: "ok", message: "" });
    mockGetPreview.mockResolvedValue({
      file_id: "file-1",
      processing_status: "NEEDS_CONFIRMATION",
      parsed_summary_json: { inferred_type: "ventas", headers: ["Fecha", "Monto", "Sucursal"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "Fecha",
        normalized_column: "fecha",
        sample_values: ["2026-03-01"],
        target_field: "transaction_date",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["1000"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Sucursal",
        normalized_column: "sucursal",
        sample_values: ["Centro"],
        target_field: "custom_field:sucursal",
        target_label: "Sucursal",
        confidence: 0,
        source: "auto_custom",
        status: "mapped",
      },
    ]);
  });

  async function confirmar() {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Confirmar importación/i })).toBeEnabled(),
    );
    // Esperar a que las sugerencias estén en pantalla: confirmar antes manda un
    // mapeo vacío (la trampa que destapó B.1).
    await screen.findByTitle("Sucursal");
    fireEvent.click(screen.getByRole("button", { name: /Confirmar importación/i }));
    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    return mockConfirmFile.mock.calls[0]![2] as ColumnMapping[];
  }

  test("el campo propio propuesto viaja con su etiqueta", async () => {
    const enviado = await confirmar();
    expect(enviado).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          source_column: "Sucursal",
          target_field: "custom_field:sucursal",
          target_label: "Sucursal",
        }),
      ]),
    );
  });

  test("una columna canónica no arrastra etiqueta", async () => {
    // El label es del campo propio. Un canónico ya tiene la suya en el
    // catálogo, y mandar otra abriría dos fuentes para el mismo nombre.
    const enviado = await confirmar();
    const fecha = enviado.find((m) => m.source_column === "Fecha")!;
    expect(fecha.target_label).toBeUndefined();
  });

  test("si la persona cambia el destino, la etiqueta vieja no lo sigue", async () => {
    renderPanel();
    await screen.findByTitle("Sucursal");
    const select = document.querySelector<HTMLSelectElement>(
      'select[data-suggests="custom_field:sucursal"]',
    );
    // El camino plano no estampa `data-suggests`; se busca el select por su valor.
    const selects = Array.from(document.querySelectorAll("select"));
    const elegido =
      select ?? selects.find((s) => s.value === "custom_field:sucursal")!;
    fireEvent.change(elegido, { target: { value: "notes" } });

    fireEvent.click(screen.getByRole("button", { name: /Confirmar importación/i }));
    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    const enviado = mockConfirmFile.mock.calls[0]![2] as ColumnMapping[];
    const sucursal = enviado.find((m) => m.source_column === "Sucursal")!;
    expect(sucursal.target_field).toBe("notes");
    // La etiqueta describía al campo propio que ya no es el destino.
    expect(sucursal.target_label).toBeUndefined();
  });
});
