import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { InventoryImpactPanel } from "../InventoryImpactPanel";
import { ingestionService } from "@/services/ingestion.service";
import type {
  InventoryImpactItem,
  InventoryReplayResult,
} from "@/services/ingestion.service";

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: { applyInventoryReplay: jest.fn() },
}));

const applyMock = ingestionService.applyInventoryReplay as jest.Mock;

const CON_VENTAS: InventoryImpactItem[] = [
  {
    product_id: "p1",
    product_name: "Vela aromática 200g",
    saldo_inicial: 10,
    saldo_final: 6,
    compradas: 0,
    vendidas: 4,
    minimo: 6,
    minimo_en: "2024-03-10",
    primer_negativo_en: null,
  },
];

const SOLO_COMPRAS: InventoryImpactItem[] = [
  { ...CON_VENTAS[0]!, vendidas: 0, compradas: 5, saldo_final: 15 },
];

function resultado(over: Partial<InventoryReplayResult> = {}): InventoryReplayResult {
  return {
    file_id: "f1",
    dry_run: false,
    aplicadas: 1,
    ya_aplicadas: 0,
    sin_stock: [],
    impacto: [],
    hojas: ["sheet:ventas"],
    alcance_por_hoja: true,
    warnings: [],
    ...over,
  };
}

beforeEach(() => {
  applyMock.mockReset();
});

describe("InventoryImpactPanel", () => {
  it("sin fileId no ofrece aplicar: el panel es sólo informativo", () => {
    render(<InventoryImpactPanel items={CON_VENTAS} total={1} />);

    expect(screen.getByText(/no se modificó/i)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("no ofrece aplicar cuando el archivo no trae ventas", () => {
    // Sólo compras: ya subieron el stock al confirmar. Un botón acá prometería
    // una operación que no tiene nada que hacer.
    render(<InventoryImpactPanel items={SOLO_COMPRAS} total={1} fileId="f1" />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  /**
   * El apply pasó a exigir hojas elegidas (review #1): el eje de inventario se
   * declara POR HOJA al importar, así que aplicar el archivo entero movería
   * stock por filas que el usuario dijo que no lo mueven. El panel las descubre
   * con el preview y el backend rechaza un apply sin selección.
   */
  async function elegirHojas() {
    fireEvent.click(screen.getByRole("button", { name: /elegir qué ventas/i }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /aplicar las ventas/i }),
      ).toBeInTheDocument(),
    );
  }

  it("aplica SÓLO las hojas elegidas y muestra lo que devolvió el servidor", async () => {
    applyMock
      .mockResolvedValueOnce(resultado({ aplicadas: 0, dry_run: true }))
      .mockResolvedValueOnce(resultado({ aplicadas: 1 }));
    render(<InventoryImpactPanel items={CON_VENTAS} total={1} fileId="f1" />);

    await elegirHojas();
    expect(applyMock).toHaveBeenNthCalledWith(1, "f1", { dryRun: true });

    fireEvent.click(screen.getByRole("button", { name: /aplicar las ventas/i }));

    await waitFor(() =>
      expect(screen.getByText(/se descontó 1 venta/i)).toBeInTheDocument(),
    );
    // La hoja viaja en el apply: sin esto el backend responde 422.
    expect(applyMock).toHaveBeenNthCalledWith(2, "f1", {
      contextIds: ["sheet:ventas"],
    });
    expect(
      screen.queryByRole("button", { name: /aplicar las ventas/i }),
    ).not.toBeInTheDocument();
  });

  it("con varias hojas no aplica hasta que se marque alguna", async () => {
    applyMock.mockResolvedValueOnce(
      resultado({ dry_run: true, hojas: ["sheet:enero", "sheet:febrero"] }),
    );
    render(<InventoryImpactPanel items={CON_VENTAS} total={1} fileId="f1" />);

    fireEvent.click(screen.getByRole("button", { name: /elegir qué ventas/i }));
    await waitFor(() =>
      expect(screen.getByLabelText("sheet:enero")).toBeInTheDocument(),
    );

    // Ninguna pre-tildada: con más de una hoja, elegir es una decisión real.
    expect(screen.getByRole("button", { name: /aplicar las ventas/i })).toBeDisabled();

    fireEvent.click(screen.getByLabelText("sheet:febrero"));
    fireEvent.click(screen.getByRole("button", { name: /aplicar las ventas/i }));

    await waitFor(() =>
      expect(applyMock).toHaveBeenNthCalledWith(2, "f1", {
        contextIds: ["sheet:febrero"],
      }),
    );
  });

  it("dice que las ventas sin stock NO se anularon y cómo seguir", async () => {
    // El punto del mensaje: la venta sigue registrada. Sin esa aclaración, "quedó
    // sin descontar" se lee como que la venta se perdió.
    applyMock.mockResolvedValueOnce(resultado({ dry_run: true })).mockResolvedValue(
      resultado({
        aplicadas: 0,
        sin_stock: [
          {
            sale_id: "s1",
            product_id: "p1",
            product_name: "Vela aromática 200g",
            quantity: 4,
            disponible: 1,
          },
        ],
      }),
    );
    render(<InventoryImpactPanel items={CON_VENTAS} total={1} fileId="f1" />);

    await elegirHojas();
    fireEvent.click(screen.getByRole("button", { name: /aplicar las ventas/i }));

    await waitFor(() =>
      expect(screen.getByText(/no se anularon/i)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/se vendieron 4 y quedaban 1/i),
    ).toBeInTheDocument();
  });
});
