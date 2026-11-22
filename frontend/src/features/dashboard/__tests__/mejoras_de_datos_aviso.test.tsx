/**
 * Mejoras de datos: el aviso muestra lo que se completó solo (con «Deshacer»),
 * ofrece «Aplicar» sólo para lo que cambia valores, y se oculta sin rol, sin
 * ruta (deploy en paralelo) o sin nada que decir.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { MejorasDeDatosAviso } from "../MejorasDeDatosAviso";
import { useAuthStore } from "@/stores/authStore";
import { PinCancelledError } from "@/stores/pinGateStore";
import type { MejorasDeDatosPanel } from "@/types/api";

const mockPanel = jest.fn();
const mockAuto = jest.fn();
const mockAplicar = jest.fn();
const mockRevertir = jest.fn();
jest.mock("@/services/mejorasDeDatos.service", () => ({
  fetchMejorasDeDatos: (...a: unknown[]) => mockPanel(...a),
  dispararMejorasAutomaticas: (...a: unknown[]) => mockAuto(...a),
  aplicarMejora: (...a: unknown[]) => mockAplicar(...a),
  revertirMejora: (...a: unknown[]) => mockRevertir(...a),
}));

const PANEL: MejorasDeDatosPanel = {
  pendientes: [
    {
      mejora_id: "cambia",
      titulo: "Corrige un valor",
      clase: "cambia_valores",
      cantidad: 2,
      en_lapida: 0,
      muestra: [{ id: "m1", etiqueta: "Yerba", detalle: {} }],
      a_mano: {},
    },
  ],
  aplicadas: [
    {
      run_id: "r1",
      mejora_id: "completa",
      titulo: "Costo de compras",
      status: "APPLIED",
      aplicadas: 3,
      creada: "2026-11-21T10:00:00Z",
      revertible: true,
      resumen: {},
    },
  ],
};

function pintar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MejorasDeDatosAviso />
    </QueryClientProvider>,
  );
}

function conRol(role: string) {
  useAuthStore.setState({
    user: { id: "u", email: "a@b.c", full_name: "A", role, tenant_id: "t" },
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockAuto.mockResolvedValue({ queued: false });
  mockPanel.mockResolvedValue(PANEL);
  conRol("OWNER");
});

test("muestra lo completado y deshace la corrida", async () => {
  mockRevertir.mockResolvedValue({ revertidos: ["x"], pendientes: {}, completa: true });
  pintar();
  expect(await screen.findByText(/Se completaron 3 datos/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Deshacer" }));
  await waitFor(() => expect(mockRevertir).toHaveBeenCalledWith("r1"));
  expect(await screen.findByText(/se deshizo la corrida/)).toBeInTheDocument();
});

test("lo que cambia valores se aplica a mano", async () => {
  mockAplicar.mockResolvedValue({ run_ids: ["r2"], aplicadas: 2, restantes: 0, a_mano: {} });
  pintar();
  fireEvent.click(await screen.findByRole("button", { name: "Aplicar" }));
  await waitFor(() => expect(mockAplicar).toHaveBeenCalledWith("cambia"));
  expect(await screen.findByText(/2 datos corregidos/)).toBeInTheDocument();
});

test("un PIN cancelado no muestra error", async () => {
  mockRevertir.mockRejectedValue(new PinCancelledError());
  pintar();
  fireEvent.click(await screen.findByRole("button", { name: "Deshacer" }));
  await waitFor(() => expect(mockRevertir).toHaveBeenCalled());
  expect(screen.queryByText(/No se pudo completar/)).not.toBeInTheDocument();
});

test("un 404 del servidor oculta el aviso en silencio", async () => {
  mockPanel.mockRejectedValue({ response: { status: 404 } });
  const { container } = pintar();
  await waitFor(() => expect(mockPanel).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});

test("un rol sin permiso no lo ve ni consulta", () => {
  conRol("VIEWER");
  const { container } = pintar();
  expect(container).toBeEmptyDOMElement();
  expect(mockPanel).not.toHaveBeenCalled();
});

test("sin nada aplicado ni pendiente no muestra nada", async () => {
  mockPanel.mockResolvedValue({ pendientes: [], aplicadas: [] });
  const { container } = pintar();
  await waitFor(() => expect(mockPanel).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});
