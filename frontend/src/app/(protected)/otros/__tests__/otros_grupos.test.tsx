/**
 * B·5a: «Otros» por grupo. El descarte pasa por la vista previa y manda su
 * token; si el grupo cambió (409 GROUP_CHANGED) se vuelve a mostrar la vista
 * previa en vez de descartar algo que el usuario no vio.
 */
import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { OthersGroups } from "../OthersGroups";
import { othersService } from "@/services/others.service";

jest.mock("@/services/others.service", () => ({
  othersService: {
    getSummary: jest.fn(),
    previewDismissGroup: jest.fn(),
    dismissGroup: jest.fn(),
  },
}));

jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) =>
    selector({ add: jest.fn() }),
}));

const svc = othersService as unknown as {
  getSummary: jest.Mock;
  previewDismissGroup: jest.Mock;
  dismissGroup: jest.Mock;
};

const GRUPOS = [
  {
    group_key: "k-total",
    uploaded_file_id: "f1",
    file_name: "ventas.xlsx",
    context_id: "sheet:Ventas",
    motivo: "Fila de total de la planilla: no es una operación",
    suggested_entity: "sale",
    status: "PENDING",
    count: 3,
  },
  {
    group_key: "k-fecha",
    uploaded_file_id: "f1",
    file_name: "ventas.xlsx",
    context_id: null,
    motivo: "Fila sin fecha reconocible",
    suggested_entity: "sale",
    status: "PENDING",
    count: 1,
  },
];

function pintar(onDismissed = jest.fn().mockResolvedValue(undefined), onSelect = jest.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <OthersGroups selected={null} onSelect={onSelect} onDismissed={onDismissed} />
    </QueryClientProvider>,
  );
  return { onDismissed, onSelect };
}

beforeEach(() => {
  jest.clearAllMocks();
  svc.getSummary.mockResolvedValue(GRUPOS);
});

test("lista los grupos con su archivo, motivo y conteo, y «Ver» filtra", async () => {
  const { onSelect } = pintar();
  expect(await screen.findByText(/Fila de total de la planilla/)).toBeInTheDocument();
  expect(screen.getByText("3")).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "Ver" })[0]!);
  expect(onSelect).toHaveBeenCalledWith(GRUPOS[0]);
});

test("descartar manda el token de la vista previa", async () => {
  svc.previewDismissGroup.mockResolvedValue({
    count: 3,
    samples: [{ detalle: "Total", monto: "100" }],
    group_token: "t".repeat(64),
  });
  svc.dismissGroup.mockResolvedValue({ dismissed: 3 });
  const { onDismissed } = pintar();
  fireEvent.click((await screen.findAllByRole("button", { name: "Descartar grupo" }))[0]!);
  fireEvent.click(await screen.findByRole("button", { name: "Descartar 3" }));
  await waitFor(() => expect(svc.dismissGroup).toHaveBeenCalledWith("k-total", "t".repeat(64)));
  await waitFor(() => expect(onDismissed).toHaveBeenCalled());
});

test("si el grupo cambió, vuelve a mostrar la vista previa y no descarta", async () => {
  svc.previewDismissGroup
    .mockResolvedValueOnce({ count: 3, samples: [], group_token: "a".repeat(64) })
    .mockResolvedValueOnce({ count: 4, samples: [], group_token: "b".repeat(64) });
  svc.dismissGroup.mockRejectedValue({
    response: { status: 409, data: { detail: { code: "GROUP_CHANGED" } } },
  });
  const { onDismissed } = pintar();
  fireEvent.click((await screen.findAllByRole("button", { name: "Descartar grupo" }))[0]!);
  fireEvent.click(await screen.findByRole("button", { name: "Descartar 3" }));
  expect(await screen.findByRole("button", { name: "Descartar 4" })).toBeInTheDocument();
  expect(svc.previewDismissGroup).toHaveBeenCalledTimes(2);
  expect(onDismissed).not.toHaveBeenCalled();
});

test("con un solo grupo pero elegido sigue visible para volver a «Ver todos»", async () => {
  svc.getSummary.mockResolvedValue([GRUPOS[1]]);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onSelect = jest.fn();
  render(
    <QueryClientProvider client={qc}>
      <OthersGroups selected="k-total" onSelect={onSelect} onDismissed={jest.fn()} />
    </QueryClientProvider>,
  );
  expect(await screen.findByText(/Fila sin fecha reconocible/)).toBeInTheDocument();
});

test("muestra la hoja del grupo", async () => {
  pintar();
  expect(await screen.findByText(/hoja Ventas/)).toBeInTheDocument();
});
