import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ColumnMapperPanel } from "../ColumnMapperPanel";
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
    confirmFile: jest.fn(),
    cancelFile: jest.fn(),
    recomputeColumnRisk: jest.fn(),
  },
}));

const mockGetPreview = ingestionService.getPreview as jest.Mock;
const mockGetColumnMappings = ingestionService.getColumnMappings as jest.Mock;
const mockConfirmFile = ingestionService.confirmFile as jest.Mock;
const mockRecomputeColumnRisk = ingestionService.recomputeColumnRisk as jest.Mock;

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
    // Default: el recompute no cambia el set (evita vaciar el panel si el
    // debounce llega a dispararse durante un test).
    mockRecomputeColumnRisk.mockResolvedValue([]);
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
      parsed_summary_json: { inferred_type: "ventas", headers: ["ColX"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
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
      parsed_summary_json: { inferred_type: "ventas", headers: ["ColX"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
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
      parsed_summary_json: { inferred_type: "ventas", headers: ["ColX"] },
      columns_at_risk: [],
    });
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "ColX",
        normalized_column: "colx",
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
      parsed_summary_json: { inferred_type: "ventas", headers: ["obs"] },
      columns_at_risk: [],
      contextual_column_risk: [risk],
    });
    mockRecomputeColumnRisk.mockResolvedValue([risk]);
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
