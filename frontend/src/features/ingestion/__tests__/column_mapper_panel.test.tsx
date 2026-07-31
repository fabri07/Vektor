import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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

  test("touched-set: resetear el mapeo (checkbox de confirmedFields) limpia el touched-set", async () => {
    // Regresión: choosePurpose() y el onChange de los checkboxes de
    // confirmedFields re-inicializan `mappings` desde las sugerencias pero
    // antes NO limpiaban touchedRef — una columna tocada a mano quedaba
    // marcada user_selected=true aunque terminara re-derivada de la sugerencia.
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

    await screen.findAllByText("ColX");

    // El usuario cambia el mapeo a mano → queda "tocado".
    fireEvent.change(screen.getAllByRole("combobox")[0]!, {
      target: { value: "quantity" },
    });

    // Togglear un checkbox de confirmedFields resetea `mappings`/`initialized`
    // (re-deriva desde la sugerencia original) — debe limpiar el touched-set.
    fireEvent.click(screen.getByRole("checkbox", { name: "gastos" }));

    // El mapeo vuelve a "amount" (la sugerencia original), no "quantity".
    await waitFor(() => {
      const select = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
      expect(select.value).toBe("amount");
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
    mockRecomputeColumnRisk.mockResolvedValue([]);
  });

  test("no arranca en Ventas: pide elegir la sección", async () => {
    mockGetPreview.mockResolvedValue(previewConHojaSinClasificar());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Ganancias")).toBeInTheDocument();
    });

    // El selector arranca en el placeholder, NO en "Ventas".
    const selector = screen.getByDisplayValue("Elegí qué es esta hoja…");
    expect(selector).toBeInTheDocument();
    // Y ofrece Productos, que antes ni siquiera era una opción.
    expect(
      screen.getByRole("option", { name: "Productos" }),
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
