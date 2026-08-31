import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { FileInterpretationReview } from "../FileInterpretationReview";
import { ingestionService } from "@/services/ingestion.service";
import type { FieldCatalog, RereadSheetStatus } from "@/services/ingestion.service";

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: {
    getFieldCatalog: jest.fn(),
    getColumnMappings: jest.fn(),
    recomputeColumnRisk: jest.fn(),
    rereadPreview: jest.fn(),
  },
}));

const mockGetFieldCatalog = ingestionService.getFieldCatalog as jest.Mock;
const mockGetColumnMappings = ingestionService.getColumnMappings as jest.Mock;
const mockRecomputeColumnRisk = ingestionService.recomputeColumnRisk as jest.Mock;
const mockRereadPreview = ingestionService.rereadPreview as jest.Mock;

const SHEETS: RereadSheetStatus[] = [
  {
    context_id: "compras",
    label: "Compras",
    entity_type: "expense",
    row_count: 10,
    status: "requiere_revision",
    columns_mapped: 2,
    columns_pending: 1,
    is_summary_or_derived: false,
  },
  {
    context_id: "ventas",
    label: "Ventas",
    entity_type: "sale",
    row_count: 5,
    status: "completa",
    columns_mapped: 3,
    columns_pending: 0,
    is_summary_or_derived: false,
  },
];

const CATALOG: FieldCatalog = {
  expense: {
    required: ["amount", "expense_date"],
    required_alternatives: {},
    fields: [
      { value: "amount", label: "Monto del gasto", single_value: true },
      { value: "expense_date", label: "Fecha del gasto", single_value: true },
      { value: "supplier_name", label: "Proveedor", single_value: false },
    ],
  },
  sale: {
    required: ["amount", "transaction_date"],
    required_alternatives: {},
    fields: [
      { value: "amount", label: "Monto de venta", single_value: true },
      { value: "transaction_date", label: "Fecha de venta", single_value: true },
    ],
  },
  product: {
    required: ["sale_price_ars"],
    required_alternatives: {},
    fields: [{ value: "sale_price_ars", label: "Precio de venta", single_value: true }],
  },
};

function renderReview(overrides: Partial<React.ComponentProps<typeof FileInterpretationReview>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onPreviewUpdated = jest.fn();
  const onError = jest.fn();
  const utils = render(
    <QueryClientProvider client={client}>
      <FileInterpretationReview
        fileId="file-1"
        runId="run-1"
        sheets={SHEETS}
        mappingContexts={[]}
        contextualColumnRisk={[]}
        impact={{
          ventas_con_producto: 0,
          ventas_sin_producto: 0,
          ventas_sin_producto_samples: [],
          compras_vinculadas: 0,
          compras_producto_nuevo: 0,
          compras_sin_producto: 0,
          compras_sin_producto_samples: [],
          compras_gate_bloqueado: 3,
          compras_gate_bloqueado_samples: [],
          movimientos_sin_producto_esperado: 0,
        }}
        onPreviewUpdated={onPreviewUpdated}
        onError={onError}
        {...overrides}
      />
    </QueryClientProvider>,
  );
  return { ...utils, onPreviewUpdated, onError };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetFieldCatalog.mockResolvedValue(CATALOG);
  mockGetColumnMappings.mockResolvedValue([
    {
      source_column: "monto",
      normalized_column: "monto",
      sample_values: ["1500", "800"],
      target_field: "amount",
      confidence: 0.9,
      source: "heuristic",
      status: "mapped",
      context_id: "compras",
    },
    {
      source_column: "proveedor",
      normalized_column: "proveedor",
      sample_values: ["Dist. Sur"],
      target_field: null,
      confidence: 0,
      source: "none",
      status: "unmapped",
      context_id: "compras",
    },
  ]);
  mockRecomputeColumnRisk.mockResolvedValue([]);
});

describe("FileInterpretationReview", () => {
  it("muestra las hojas, sus columnas y el impacto proyectado", async () => {
    renderReview();

    // Las dos hojas aparecen como pestañas.
    expect(screen.getByRole("tab", { name: /Compras/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Ventas/i })).toBeInTheDocument();

    // La primera hoja arranca activa: sus columnas se piden y se muestran.
    await waitFor(() =>
      expect(mockGetColumnMappings).toHaveBeenCalledWith("file-1", "expense", "compras", "run-1"),
    );
    expect(await screen.findByText("monto")).toBeInTheDocument();
    expect(screen.getByText("proveedor")).toBeInTheDocument();

    // Impacto proyectado (5 categorías): solo se muestra lo que es > 0.
    expect(screen.getByText(/Compras bloqueadas/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("corregir un mapeo y actualizar la vista previa persiste la corrección", async () => {
    const user = userEvent.setup();
    mockRereadPreview.mockResolvedValue({
      file_id: "file-1",
      run_id: "run-1",
      draft_version: 1,
      status: "READY_TO_APPLY",
      counts: {
        to_update: 0,
        preserved: 0,
        new: 0,
        to_void: 0,
        unchanged: 0,
        products_new: 0,
        products_restock: 0,
      },
      impact: {
        ventas_con_producto: 0,
        ventas_sin_producto: 0,
        ventas_sin_producto_samples: [],
        compras_vinculadas: 0,
        compras_producto_nuevo: 0,
        compras_sin_producto: 0,
        compras_sin_producto_samples: [],
        compras_gate_bloqueado: 0,
        compras_gate_bloqueado_samples: [],
        movimientos_sin_producto_esperado: 0,
      },
      sheets: SHEETS,
      mapping_contexts: [],
      contextual_column_risk: [],
      legacy_fallback: false,
      sample_changes: [],
    });

    const { onPreviewUpdated } = renderReview();

    // Corrige "proveedor" (sin mapear) a "supplier_name". El select de
    // "Sección de la hoja" (nuevo, C3) tiene aria-label propio — se filtran
    // acá los selects de mapeo de columna, que no tienen nombre accesible.
    const selects = await screen.findAllByRole("combobox", { name: "" });
    expect(selects).toHaveLength(2);
    // El segundo select corresponde a la fila "proveedor" (la primera es "monto").
    await user.selectOptions(selects[1]!, "supplier_name");

    const updateButton = await screen.findByRole("button", {
      name: /actualizar vista previa/i,
    });
    await user.click(updateButton);

    await waitFor(() => expect(mockRereadPreview).toHaveBeenCalledTimes(1));
    const [fileId, draft] = mockRereadPreview.mock.calls[0];
    expect(fileId).toBe("file-1");
    expect(draft.columnMappings).toEqual([
      {
        source_column: "proveedor",
        target_field: "supplier_name",
        context_id: "compras",
        user_selected: true,
      },
    ]);
    // `context_confirmed` cubre TODAS las hojas (no solo la corregida) —
    // sin esto una hoja no visitada quedaría excluida en el backend en
    // cuanto el dict deja de estar vacío.
    expect(draft.contextConfirmed).toEqual({ compras: true, ventas: true });

    await waitFor(() => expect(onPreviewUpdated).toHaveBeenCalledWith(
      expect.objectContaining({ draft_version: 1 }),
    ));
  });

  it("permite reasignar la entidad de una hoja completa (C3)", async () => {
    const user = userEvent.setup();
    mockRereadPreview.mockResolvedValue({
      file_id: "file-1",
      run_id: "run-1",
      draft_version: 1,
      status: "READY_TO_APPLY",
      counts: {
        to_update: 0,
        preserved: 0,
        new: 0,
        to_void: 0,
        unchanged: 0,
        products_new: 0,
        products_restock: 0,
      },
      impact: {
        ventas_con_producto: 0,
        ventas_sin_producto: 0,
        ventas_sin_producto_samples: [],
        compras_vinculadas: 0,
        compras_producto_nuevo: 0,
        compras_sin_producto: 0,
        compras_sin_producto_samples: [],
        compras_gate_bloqueado: 0,
        compras_gate_bloqueado_samples: [],
        movimientos_sin_producto_esperado: 0,
      },
      sheets: SHEETS,
      mapping_contexts: [],
      contextual_column_risk: [],
      legacy_fallback: false,
      sample_changes: [],
    });

    renderReview();

    const entitySelect = await screen.findByRole("combobox", {
      name: /Sección de la hoja Compras/i,
    });
    await user.selectOptions(entitySelect, "product");

    // Reasignar la hoja re-pide el mapeo de columnas contra la entidad NUEVA
    // (el catálogo de "producto" no es el de "gasto").
    await waitFor(() =>
      expect(mockGetColumnMappings).toHaveBeenCalledWith("file-1", "product", "compras", "run-1"),
    );

    const updateButton = await screen.findByRole("button", {
      name: /actualizar vista previa/i,
    });
    await user.click(updateButton);

    await waitFor(() => expect(mockRereadPreview).toHaveBeenCalledTimes(1));
    const [, draft] = mockRereadPreview.mock.calls[0];
    expect(draft.contextEntity).toEqual({ compras: "product" });
  });

  it("permite excluir una hoja de la relectura (C3)", async () => {
    const user = userEvent.setup();
    mockRereadPreview.mockResolvedValue({
      file_id: "file-1",
      run_id: "run-1",
      draft_version: 1,
      status: "READY_TO_APPLY",
      counts: {
        to_update: 0,
        preserved: 0,
        new: 0,
        to_void: 0,
        unchanged: 0,
        products_new: 0,
        products_restock: 0,
      },
      impact: {
        ventas_con_producto: 0,
        ventas_sin_producto: 0,
        ventas_sin_producto_samples: [],
        compras_vinculadas: 0,
        compras_producto_nuevo: 0,
        compras_sin_producto: 0,
        compras_sin_producto_samples: [],
        compras_gate_bloqueado: 0,
        compras_gate_bloqueado_samples: [],
        movimientos_sin_producto_esperado: 0,
      },
      sheets: SHEETS,
      mapping_contexts: [],
      contextual_column_risk: [],
      legacy_fallback: false,
      sample_changes: [],
    });

    renderReview();

    const includeCheckbox = await screen.findByRole("checkbox", {
      name: /Incluir esta hoja en la relectura/i,
    });
    expect(includeCheckbox).toBeChecked();
    await user.click(includeCheckbox);

    const updateButton = await screen.findByRole("button", {
      name: /actualizar vista previa/i,
    });
    await user.click(updateButton);

    await waitFor(() => expect(mockRereadPreview).toHaveBeenCalledTimes(1));
    const [, draft] = mockRereadPreview.mock.calls[0];
    // "compras" (la hoja activa) queda excluida; "ventas" sigue incluida
    // aunque el usuario no la haya tocado.
    expect(draft.contextConfirmed).toEqual({ compras: false, ventas: true });
  });

  it("muestra el tratamiento de stock para una hoja de productos y lo persiste (C3)", async () => {
    const user = userEvent.setup();
    const sheetsConProducto: RereadSheetStatus[] = [
      {
        context_id: "catalogo",
        label: "Catálogo",
        entity_type: "product",
        row_count: 8,
        status: "completa",
        columns_mapped: 2,
        columns_pending: 0,
        is_summary_or_derived: false,
      },
    ];
    mockGetColumnMappings.mockResolvedValue([]);
    mockRereadPreview.mockResolvedValue({
      file_id: "file-1",
      run_id: "run-1",
      draft_version: 1,
      status: "READY_TO_APPLY",
      counts: {
        to_update: 0,
        preserved: 0,
        new: 0,
        to_void: 0,
        unchanged: 0,
        products_new: 0,
        products_restock: 0,
      },
      impact: {
        ventas_con_producto: 0,
        ventas_sin_producto: 0,
        ventas_sin_producto_samples: [],
        compras_vinculadas: 0,
        compras_producto_nuevo: 0,
        compras_sin_producto: 0,
        compras_sin_producto_samples: [],
        compras_gate_bloqueado: 0,
        compras_gate_bloqueado_samples: [],
        movimientos_sin_producto_esperado: 0,
      },
      sheets: sheetsConProducto,
      mapping_contexts: [],
      contextual_column_risk: [],
      legacy_fallback: false,
      sample_changes: [],
    });

    renderReview({ sheets: sheetsConProducto });

    const compraButton = await screen.findByRole("button", { name: /la compré ahora/i });
    await user.click(compraButton);

    const updateButton = await screen.findByRole("button", {
      name: /actualizar vista previa/i,
    });
    await user.click(updateButton);

    await waitFor(() => expect(mockRereadPreview).toHaveBeenCalledTimes(1));
    const [, draft] = mockRereadPreview.mock.calls[0];
    expect(draft.stockTreatment).toEqual({ catalogo: "purchase" });
  });

  it("navega entre hojas con Anterior/Siguiente y muestra 'Hoja X de N'", async () => {
    const user = userEvent.setup();
    renderReview();

    expect(screen.getByText("Hoja 1 de 2")).toBeInTheDocument();
    const next = screen.getByRole("button", { name: /siguiente/i });
    const prev = screen.getByRole("button", { name: /anterior/i });
    expect(prev).toBeDisabled();

    await user.click(next);
    expect(screen.getByText("Hoja 2 de 2")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Ventas/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(next).toBeDisabled();

    await user.click(prev);
    expect(screen.getByText("Hoja 1 de 2")).toBeInTheDocument();
  });

  it("muestra una hoja derivada como excluida por defecto con su explicación", async () => {
    const sheetsConDerivada: RereadSheetStatus[] = [
      ...SHEETS,
      {
        context_id: "ganancias",
        label: "Ganancias",
        entity_type: "otros",
        row_count: 4,
        status: "ignorada",
        columns_mapped: 0,
        columns_pending: 0,
        is_summary_or_derived: true,
      },
    ];
    renderReview({ sheets: sheetsConDerivada });

    const tab = screen.getByRole("tab", { name: /Ganancias/i });
    expect(tab).toBeInTheDocument();
    expect(screen.getByText("Derivada")).toBeInTheDocument();

    await userEvent.setup().click(tab);
    expect(
      screen.getByText(/Véktor calcula solo desde tus movimientos/i),
    ).toBeInTheDocument();
    const includeCheckbox = screen.getByRole("checkbox", {
      name: /Incluir esta hoja en la relectura/i,
    });
    expect(includeCheckbox).not.toBeChecked();
  });

  it("muestra un nombre legible para una columna sin encabezado (col_N)", async () => {
    mockGetColumnMappings.mockResolvedValue([
      {
        source_column: "col_3",
        normalized_column: "col_3",
        sample_values: ["1500"],
        target_field: "amount",
        confidence: 0.5,
        source: "heuristic",
        status: "mapped",
        context_id: "compras",
      },
    ]);

    renderReview();

    expect(await screen.findByText("Columna sin encabezado 3")).toBeInTheDocument();
    expect(screen.queryByText("col_3")).not.toBeInTheDocument();
  });

  describe("Bloque 5 (consumo) — decisiones recordadas", () => {
    const sheetsConRecordado: RereadSheetStatus[] = [
      {
        ...SHEETS[0]!,
        remembered_decisions: {
          column_mapping: { mapping: { proveedor: "supplier_name" } },
          context_included: { included: true },
        },
      },
      SHEETS[1]!,
    ];

    it("precarga el mapeo recordado, lo muestra como tal, y sigue siendo editable", async () => {
      const user = userEvent.setup();
      renderReview({ sheets: sheetsConRecordado });

      // Precargado: "proveedor" (unmapped en la sugerencia) ya aparece con
      // el destino recordado, sin que el usuario haya tocado nada.
      const selects = await screen.findAllByRole("combobox", { name: "" });
      expect(selects[1]).toHaveValue("supplier_name");

      // Mostrado claramente como recordado, no como "elegido ahora".
      expect(
        screen.getByText(/Recordado de una carga anterior con este mismo formato/i),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/Precargamos el mapeo y la sección de una carga anterior/i),
      ).toBeInTheDocument();

      // Editable: el usuario lo cambia y el cambio se respeta.
      await user.selectOptions(selects[1]!, "amount");
      expect(selects[1]).toHaveValue("amount");
      expect(
        screen.queryByText(/Recordado de una carga anterior con este mismo formato/i),
      ).not.toBeInTheDocument();
    });

    it("no se aplica silenciosamente: recién actualiza el preview cuando el usuario confirma", async () => {
      const user = userEvent.setup();
      mockRereadPreview.mockResolvedValue({
        file_id: "file-1",
        run_id: "run-1",
        draft_version: 2,
        status: "READY_TO_APPLY",
        counts: {
          to_update: 0,
          preserved: 0,
          new: 0,
          to_void: 0,
          unchanged: 0,
          products_new: 0,
          products_restock: 0,
        },
        impact: {
          ventas_con_producto: 0,
          ventas_sin_producto: 0,
          ventas_sin_producto_samples: [],
          compras_vinculadas: 0,
          compras_producto_nuevo: 0,
          compras_sin_producto: 0,
          compras_sin_producto_samples: [],
          compras_gate_bloqueado: 0,
          compras_gate_bloqueado_samples: [],
          movimientos_sin_producto_esperado: 0,
        },
        sheets: sheetsConRecordado,
        mapping_contexts: [],
        contextual_column_risk: [],
        legacy_fallback: false,
        sample_changes: [],
      });

      renderReview({ sheets: sheetsConRecordado });

      // Lo recordado ya está visible en pantalla, pero todavía no se mandó nada.
      await screen.findAllByRole("combobox", { name: "" });
      expect(mockRereadPreview).not.toHaveBeenCalled();

      const updateButton = await screen.findByRole("button", {
        name: /actualizar vista previa/i,
      });
      await user.click(updateButton);

      // Recién ACÁ se manda lo recordado — como una corrección más del borrador.
      await waitFor(() => expect(mockRereadPreview).toHaveBeenCalledTimes(1));
      const [, draft] = mockRereadPreview.mock.calls[0];
      expect(draft.columnMappings).toContainEqual(
        expect.objectContaining({
          source_column: "proveedor",
          target_field: "supplier_name",
          context_id: "compras",
        }),
      );
    });
  });
});
