/**
 * Seguridad: que la pantalla no afirme lo que no sabe.
 *
 * - La tarjeta del PIN tiene tres estados. Mientras carga no puede decir
 *   "no configuraste un PIN" (el dueño que sí lo tenía terminaba en un
 *   formulario inútil).
 * - Habilitar una caja muestra lo que contestó el servidor; cancelar el PIN no
 *   es un error. Antes todo decía "¿nombre repetido?".
 * - Una lista de cajas que no se pudo leer no se muestra como lista vacía.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { SecurityPanel } from "../SecurityPanel";
import { TerminalsSection } from "../TerminalsSection";
import { posService } from "@/services/pos.service";
import { securityService } from "@/services/security.service";
import { useAuthStore } from "@/stores/authStore";
import { PinCancelledError, usePinGateStore } from "@/stores/pinGateStore";
import { useToastStore } from "@/stores/toastStore";

jest.mock("@/services/security.service", () => ({
  securityService: {
    pinStatus: jest.fn(),
    listTeam: jest.fn().mockResolvedValue([]),
  },
}));
jest.mock("@/services/users.service", () => ({ createTeamUserRequest: jest.fn() }));
jest.mock("@/services/pos.service", () => ({
  posService: {
    listTerminals: jest.fn(),
    enrollTerminal: jest.fn(),
    disableTerminal: jest.fn(),
  },
}));

const pinStatus = securityService.pinStatus as jest.Mock;
const pos = posService as jest.Mocked<typeof posService>;

function conQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function respuesta(status: number, detail: unknown): AxiosError {
  const e = new AxiosError(`status ${status}`);
  e.response = {
    status,
    data: { detail },
    headers: {},
    statusText: "",
    config: {} as InternalAxiosRequestConfig,
  };
  return e;
}

beforeEach(() => {
  jest.clearAllMocks();
  useToastStore.setState({ toasts: [] });
  useAuthStore.setState({
    token: "t",
    user: { id: "o", email: "o@b.com", full_name: "Dueño", role: "OWNER", tenant_id: "t1" },
  });
  usePinGateStore.setState({ isOpen: false, mode: "verify" });
});

describe("tarjeta del PIN", () => {
  it("mientras carga no dice que no hay PIN", () => {
    pinStatus.mockReturnValue(new Promise(() => {}));
    conQuery(<SecurityPanel />);
    expect(screen.getByLabelText("Cargando el estado del PIN")).toBeInTheDocument();
    expect(screen.queryByText("Todavía no configuraste un PIN.")).not.toBeInTheDocument();
  });

  it("si no se pudo leer el estado lo dice, y ofrece reintentar", async () => {
    pinStatus.mockRejectedValue(new Error("caído"));
    conQuery(<SecurityPanel />);
    expect(
      await screen.findByText("No se pudo cargar el estado de tu PIN."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Todavía no configuraste un PIN.")).not.toBeInTheDocument();
    pinStatus.mockResolvedValue({ pin_set: true, verified: false, must_set: false });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    });
    expect(await screen.findByLabelText("PIN actual")).toBeInTheDocument();
  });

  it("con PIN configurado ofrece recuperarlo con la contraseña", async () => {
    pinStatus.mockResolvedValue({ pin_set: true, verified: false, must_set: false });
    conQuery(<SecurityPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "¿Olvidaste tu PIN?" }));
    expect(usePinGateStore.getState()).toMatchObject({ isOpen: true, mode: "reset" });
  });
});

describe("cajas habilitadas", () => {
  async function abrirYHabilitar() {
    conQuery(<TerminalsSection />);
    fireEvent.click(screen.getByRole("button", { name: "Administrar cajas" }));
    const nombre = await screen.findByPlaceholderText("Nombre de esta caja (ej. Mostrador)");
    fireEvent.change(nombre, { target: { value: "Mostrador" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Habilitar esta PC" }));
    });
  }

  it("cancelar el PIN no muestra ningún error", async () => {
    pos.listTerminals.mockResolvedValue([]);
    pos.enrollTerminal.mockRejectedValue(new PinCancelledError());
    await abrirYHabilitar();
    await waitFor(() => expect(pos.enrollTerminal).toHaveBeenCalled());
    expect(useToastStore.getState().toasts).toEqual([]);
  });

  it("muestra el motivo que dio el servidor, no uno adivinado", async () => {
    pos.listTerminals.mockResolvedValue([]);
    pos.enrollTerminal.mockRejectedValue(
      respuesta(409, {
        code: "TERMINAL_NAME_TAKEN",
        message: "Ya hay una caja con ese nombre.",
      }),
    );
    await abrirYHabilitar();
    await waitFor(() => expect(useToastStore.getState().toasts).toHaveLength(1));
    expect(useToastStore.getState().toasts[0]!.message).toBe(
      "No se pudo habilitar la caja. Ya hay una caja con ese nombre.",
    );
  });

  it("un fallo que no es el nombre no se presenta como nombre repetido", async () => {
    pos.listTerminals.mockResolvedValue([]);
    pos.enrollTerminal.mockRejectedValue(respuesta(500, "Internal Server Error"));
    await abrirYHabilitar();
    await waitFor(() => expect(useToastStore.getState().toasts).toHaveLength(1));
    expect(useToastStore.getState().toasts[0]!.message).not.toMatch(/repetido/);
  });

  it("una lista que no se pudo leer por el PIN no se muestra como vacía", async () => {
    pos.listTerminals.mockRejectedValue(new PinCancelledError());
    conQuery(<TerminalsSection />);
    fireEvent.click(screen.getByRole("button", { name: "Administrar cajas" }));
    expect(await screen.findByText("Para ver las cajas hace falta tu PIN.")).toBeInTheDocument();
    expect(screen.queryByText("Todavía no hay cajas habilitadas.")).not.toBeInTheDocument();
  });
});
