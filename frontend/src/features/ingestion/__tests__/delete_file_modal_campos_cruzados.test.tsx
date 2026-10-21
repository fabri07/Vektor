/**
 * B·6: el aviso previo al borrado dice cuántos datos de fichas de clientes o
 * proveedores vuelven a quedar vacíos, y cuáles se conservan porque el usuario
 * los cambió después (`campo_modificado_posteriormente`, con sus campos).
 */

import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DeleteFileModal } from "../DeleteFileModal";
import { ingestionService, type FileDeletionPreview } from "@/services/ingestion.service";

jest.mock("@/services/ingestion.service", () => ({
  ingestionService: { getDeletionPreview: jest.fn() },
}));

const mockPreview = ingestionService.getDeletionPreview as jest.Mock;

const base: FileDeletionPreview = {
  file_id: "f1",
  ventas: 0,
  gastos: 0,
  productos: 0,
  movimientos_stock: 0,
  otros: 0,
  otros_ya_clasificados: 0,
  has_user_edits: false,
  productos_no_rastreables: false,
  productos_a_restaurar: 0,
  conservados: [],
};

function renderModal() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DeleteFileModal
        fileId="f1"
        filename="ventas.xlsx"
        isDeleting={false}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    </QueryClientProvider>,
  );
}

describe("DeleteFileModal — campos de ficha (B·6)", () => {
  it("avisa cuántos datos de fichas vuelven a vacío y no dice que no hay datos", async () => {
    mockPreview.mockResolvedValue({ ...base, campos_cruzados_a_restaurar: 2 });
    renderModal();

    expect(
      await screen.findByText(/2 datos que este archivo completó en fichas/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no tiene datos importados/)).not.toBeInTheDocument();
  });

  it("muestra el campo que se conserva por haberlo cambiado después", async () => {
    mockPreview.mockResolvedValue({
      ...base,
      campos_cruzados_a_restaurar: 1,
      conservados: [
        {
          entity_type: "customer",
          id: "c1",
          name: "Cliente Uno",
          reasons: ["campo_modificado_posteriormente"],
          fields: ["address"],
        },
      ],
    });
    renderModal();

    expect(await screen.findByText("Cliente Uno")).toBeInTheDocument();
    expect(
      screen.getByText(/cambiaste estos campos después de importarlo \(address\)/),
    ).toBeInTheDocument();
  });

  it("un backend anterior sin el campo no rompe ni avisa", async () => {
    mockPreview.mockResolvedValue({ ...base, ventas: 1 });
    renderModal();

    expect(await screen.findByText("Ventas")).toBeInTheDocument();
    expect(screen.queryByText(/fichas de clientes o proveedores/)).not.toBeInTheDocument();
  });
});
