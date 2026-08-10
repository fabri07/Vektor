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
 * F-H3.e — lo que el backend ofrece para una hoja de ventas con producto y
 * cantidad mapeados. Las opciones y sus textos los sirve `/inventory-effects`:
 * el frontend no tiene lista propia.
 */
const EFECTOS_VENTAS = [
  {
    context_id: "table",
    label: "Ventas",
    default: "informational",
    options: [
      { value: "informational", label: "Estas filas no modificarán el inventario" },
      { value: "historical_replay", label: "Aplicar la historia: las compras suman y las ventas restan" },
      { value: "no_inventory", label: "Estas cantidades no afectan el inventario" },
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
      { value: "amount", label: "Monto de venta", single_value: true },
      { value: "transaction_date", label: "Fecha de venta", single_value: true },
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

  test("muestra el source 'llm' como IA con su confianza", async () => {
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

    await waitFor(() => {
      expect(screen.getByText(/IA/)).toBeInTheDocument();
    });
    expect(screen.getByText(/80%/)).toBeInTheDocument();
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
    // source llm visible en al menos una sección de hoja.
    await waitFor(() => {
      expect(screen.getAllByText(/IA/).length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText(/77%/).length).toBeGreaterThan(0);
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
   * F-H3.e — la regresión de la fase: el panel NO mandaba `inventory_effect`, así
   * que `historical_replay` (el único modo que escribe stock) era inalcanzable
   * desde la pantalla y todo archivo entraba con el default de cada hoja. Todo lo
   * demás estaba cableado y probado punta a punta contra el endpoint.
   */
  test("el efecto de inventario elegido viaja en el confirm", async () => {
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

    // El modo se elige con el texto que mandó el backend, no con uno de acá.
    const aplicar = await screen.findByRole("button", {
      name: EFECTOS_VENTAS[0]!.options[1]!.label,
    });
    fireEvent.click(aplicar);
    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // `inventory_effect` es el 8º argumento posicional (índice 7).
    expect(mockConfirmFile.mock.calls[0][7]).toEqual({
      table: "historical_replay",
    });
  });

  test("sin tocar nada viaja el default que muestra la pantalla", async () => {
    // Si el panel mandara `undefined`, el backend aplicaría su propio default y
    // coincidiría por casualidad. Mandarlo explícito es lo que hace que el
    // `STAGE_CONFIRM` registre con qué modo entró cada hoja.
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

    // El selector ya está: recién ahí el panel sabe qué efecto mandar.
    await screen.findByRole("button", {
      name: EFECTOS_VENTAS[0]!.options[0]!.label,
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Confirmar importación/i }),
    );

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    expect(mockConfirmFile.mock.calls[0][7]).toEqual({ table: "informational" });
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

/**
 * F-H6.c — que la decisión de costo LLEGUE al servicio.
 *
 * Los tests de `PurchaseCostChoice` prueban el componente y los de
 * `ingestion_confirm_payload` prueban el cuerpo del POST. Falta el tramo del
 * medio: que el panel arme el payload y se lo pase. Las mutaciones lo mostraron
 * — sacar `costoPayload` de la llamada dejaba todo en verde.
 */
describe("ColumnMapperPanel — F-H6.c: la decisión de costo llega al confirm", () => {
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
        inferred_type: "gastos",
        headers: ["Fecha", "Monto", "Descuento"],
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
      },
      {
        source_column: "Monto",
        normalized_column: "monto",
        sample_values: ["12000"],
        target_field: "amount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
      {
        source_column: "Descuento",
        normalized_column: "descuento",
        sample_values: ["2000"],
        target_field: "discount",
        confidence: 0.9,
        source: "heuristic",
        status: "mapped",
      },
    ]);
  });

  test("el selector aparece porque la hoja mapea un descuento", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/ya incluye el descuento/i)).toBeInTheDocument();
    });
  });

  test("declarar que el monto es bruto viaja en el confirm", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /El monto es el bruto/ })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("radio", { name: /El monto es el bruto/ }));
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    const decisiones = mockConfirmFile.mock.calls[0]?.[9];
    expect(decisiones).toEqual([
      {
        context_id: "",
        base: "monto_sin_ajustes",
        shared_shipping: "no_distribuir",
        line_shipping: "gasto_aparte",
      },
    ]);
  });

  test("sin tocar nada no se manda ninguna decisión", async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/ya incluye el descuento/i)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /Confirmar/i }));

    await waitFor(() => expect(mockConfirmFile).toHaveBeenCalled());
    // `undefined`, no una lista con los defaults: mandar lo que el backend ya
    // aplica solo es ruido, y omitirlo deja claro en la traza que no hubo decisión.
    expect(mockConfirmFile.mock.calls[0]?.[9]).toBeUndefined();
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
