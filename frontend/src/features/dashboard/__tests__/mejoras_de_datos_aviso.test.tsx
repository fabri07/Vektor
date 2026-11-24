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
import type { MejoraVistaPrevia, MejorasDeDatosPanel } from "@/types/api";

const mockPanel = jest.fn();
const mockAuto = jest.fn();
const mockAplicar = jest.fn();
const mockRevertir = jest.fn();
const mockVistaPrevia = jest.fn();
jest.mock("@/services/mejorasDeDatos.service", () => ({
  fetchMejorasDeDatos: (...a: unknown[]) => mockPanel(...a),
  dispararMejorasAutomaticas: (...a: unknown[]) => mockAuto(...a),
  aplicarMejora: (...a: unknown[]) => mockAplicar(...a),
  revertirMejora: (...a: unknown[]) => mockRevertir(...a),
  fetchVistaPrevia: (...a: unknown[]) => mockVistaPrevia(...a),
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

const VISTA: MejoraVistaPrevia = {
  filas: [
    {
      id: "f1",
      etiqueta: "Camisa lino beige M",
      qty: 8,
      total: "48000",
      costo_unitario: "6000.00",
      fecha: "2026-11-14T10:00:00",
      medio_de_pago: "cash",
      medio_por_defecto: false,
      ventas_a_costear: 2,
      arqueo_del_dia: true,
    },
    {
      id: "f2",
      etiqueta: "Pantalón cargo verde T42",
      qty: 15,
      total: "90000",
      costo_unitario: "6000.00",
      fecha: "2026-11-14T10:00:00",
      medio_de_pago: "transfer",
      medio_por_defecto: true,
      ventas_a_costear: 0,
      arqueo_del_dia: false,
    },
  ],
  a_mano: { "Remera X (1a2b3c4d)": "no se encontró la fila en el archivo" },
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
  mockVistaPrevia.mockResolvedValue(VISTA);
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

async function abrirVistaPrevia() {
  fireEvent.click(await screen.findByRole("button", { name: "Ver y aplicar" }));
  return screen.findByRole("list", { name: "Qué se va a cambiar" });
}

test("lo que cambia valores muestra su vista previa antes de aplicar", async () => {
  pintar();
  await abrirVistaPrevia();
  expect(mockVistaPrevia).toHaveBeenCalledWith("cambia");
  // Nada se aplicó sólo por abrirla.
  expect(mockAplicar).not.toHaveBeenCalled();
  expect(screen.getByText("Camisa lino beige M")).toBeInTheDocument();
  expect(screen.getByText(/Total pagado\s+\$\s?48\.000,00/)).toBeInTheDocument();
  // Las dos filas salen a $ 6.000,00 por unidad.
  expect(screen.getAllByText(/costo por unidad\s+\$\s?6\.000,00/)).toHaveLength(2);
  expect(screen.getAllByText(/14\/11\/2026/).length).toBe(2);
  expect(screen.getByText(/Efectivo$/)).toBeInTheDocument();
  expect(screen.getByText(/Transferencia \(el archivo no lo decía\)/)).toBeInTheDocument();
  expect(screen.getByText(/Se costean 2 ventas/)).toBeInTheDocument();
});

test("no vuelve a leer los archivos al volver el foco a la ventana", async () => {
  pintar();
  await abrirVistaPrevia();
  expect(mockVistaPrevia).toHaveBeenCalledTimes(1);
  // Volver a la pestaña no baja y parsea otra vez los archivos ni cambia las filas.
  window.dispatchEvent(new Event("visibilitychange"));
  window.dispatchEvent(new Event("focus"));
  await new Promise((r) => setTimeout(r, 20));
  expect(mockVistaPrevia).toHaveBeenCalledTimes(1);
});

test("si la cantidad depende del archivo, el aviso dice «hasta N»", async () => {
  mockPanel.mockResolvedValue({
    ...PANEL,
    pendientes: [{ ...PANEL.pendientes[0], necesita_archivo: true }],
  });
  pintar();
  expect(
    await screen.findByText(/hasta 2 datos podrían cambiar \(se confirma al leer tus archivos\)/),
  ).toBeInTheDocument();
});

test("sin archivo de por medio, el aviso cuenta lo que cambiaría", async () => {
  pintar();
  expect(await screen.findByText(/Corrige un valor: 2 datos cambiarían/)).toBeInTheDocument();
});

test("avisa del arqueo sólo en la fila que lo tiene", async () => {
  pintar();
  await abrirVistaPrevia();
  expect(screen.getAllByRole("note")).toHaveLength(1);
  expect(screen.getByRole("note")).toHaveTextContent(/ya tiene arqueo/);
});

test("lo que no se pudo verificar se lista con su motivo y no se manda", async () => {
  mockAplicar.mockResolvedValue({ run_ids: ["r2"], aplicadas: 2, restantes: 0, a_mano: {} });
  pintar();
  await abrirVistaPrevia();
  expect(screen.getByText(/1 dato no se puede verificar y no se toca/)).toBeInTheDocument();
  expect(screen.getByText(/no se encontró la fila en el archivo/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Aplicar" }));
  // Sólo los ids que se mostraron.
  await waitFor(() => expect(mockAplicar).toHaveBeenCalledWith("cambia", ["f1", "f2"]));
  expect(await screen.findByText(/2 datos corregidos/)).toBeInTheDocument();
  // La vista previa se cierra al aplicar.
  expect(screen.queryByRole("list", { name: "Qué se va a cambiar" })).not.toBeInTheDocument();
});

test("sin filas aplicables no ofrece aplicar", async () => {
  mockVistaPrevia.mockResolvedValue({ filas: [], a_mano: { X: "el archivo original ya no está" } });
  pintar();
  fireEvent.click(await screen.findByRole("button", { name: "Ver y aplicar" }));
  expect(await screen.findByText(/No hay nada para aplicar hoy/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Aplicar" })).not.toBeInTheDocument();
});

test("si la vista previa falla muestra el motivo y se puede cerrar", async () => {
  mockVistaPrevia.mockRejectedValue({ response: { data: { detail: "Mejora no encontrada." } } });
  pintar();
  fireEvent.click(await screen.findByRole("button", { name: "Ver y aplicar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Mejora no encontrada.");
  fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));
  expect(screen.getByRole("button", { name: "Ver y aplicar" })).toBeInTheDocument();
});

test("un PIN cancelado al aplicar no muestra error y deja la vista previa", async () => {
  mockAplicar.mockRejectedValue(new PinCancelledError());
  pintar();
  await abrirVistaPrevia();
  fireEvent.click(screen.getByRole("button", { name: "Aplicar" }));
  await waitFor(() => expect(mockAplicar).toHaveBeenCalled());
  expect(screen.queryByText(/No se pudo completar/)).not.toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Qué se va a cambiar" })).toBeInTheDocument();
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
