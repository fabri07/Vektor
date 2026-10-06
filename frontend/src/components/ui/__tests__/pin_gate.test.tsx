/**
 * El PIN de step-up: cancelar, y recuperarlo con la contraseña.
 *
 * Lo que se protege:
 * - Cancelar el modal rechaza con `PinCancelledError`, no con el 428 original:
 *   con el 428 cada pantalla inventaba una causa ("¿nombre repetido?").
 * - «¿Olvidaste tu PIN?» lo restablece con la contraseña, abre la ventana con
 *   el PIN nuevo y la acción que estaba esperando se retoma UNA sola vez.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AxiosError, InternalAxiosRequestConfig } from "axios";

jest.mock("@sentry/nextjs", () => ({ captureException: jest.fn(), addBreadcrumb: jest.fn() }));
jest.mock("@/services/security.service", () => ({
  securityService: {
    pinStatus: jest.fn().mockResolvedValue({ pin_set: true, verified: false, must_set: false }),
    verifyPin: jest.fn().mockResolvedValue(undefined),
    setupPin: jest.fn(),
    resetPin: jest.fn().mockResolvedValue(undefined),
  },
}));

import { PinGateModal } from "@/components/ui/PinGateModal";
import { api, handleApiResponseError } from "@/lib/api";
import { securityService } from "@/services/security.service";
import { esPinCancelado, usePinGateStore } from "@/stores/pinGateStore";

const svc = securityService as jest.Mocked<typeof securityService>;

function pinRequerido(): AxiosError {
  return {
    isAxiosError: true,
    name: "AxiosError",
    message: "428",
    config: { method: "post", url: "/pos/terminals", headers: {} } as InternalAxiosRequestConfig,
    response: {
      status: 428,
      data: { detail: "PIN_REQUIRED" },
      headers: {},
      statusText: "",
      config: {} as InternalAxiosRequestConfig,
    },
    toJSON: () => ({}),
  } as unknown as AxiosError;
}

function montarModal() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PinGateModal />
    </QueryClientProvider>,
  );
}

let request: jest.SpyInstance;

beforeEach(() => {
  jest.clearAllMocks();
  usePinGateStore.setState({
    isOpen: false,
    mode: "verify",
    _resolve: null,
    _reject: null,
    _promise: null,
  });
  request = jest.spyOn(api, "request").mockResolvedValue({ data: { ok: true } } as never);
});

afterEach(() => {
  request.mockRestore();
});

it("cancelar el PIN rechaza con la cancelación, no con el 428, y no reintenta", async () => {
  const pendiente = handleApiResponseError(pinRequerido());
  await waitFor(() => expect(usePinGateStore.getState().isOpen).toBe(true));
  act(() => usePinGateStore.getState().cancel());
  const motivo = await pendiente.catch((e: unknown) => e);
  expect(esPinCancelado(motivo)).toBe(true);
  expect(request).not.toHaveBeenCalled();
});

it("PIN olvidado: restablece con la contraseña, verifica el nuevo y retoma la acción una vez", async () => {
  montarModal();
  const pendiente = handleApiResponseError(pinRequerido());
  await screen.findByText("Ingresá tu PIN");

  fireEvent.click(screen.getByRole("button", { name: "¿Olvidaste tu PIN?" }));
  expect(await screen.findByText("Restablecé tu PIN")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Contraseña de tu cuenta"), {
    target: { value: "Secreta123" },
  });
  fireEvent.change(screen.getByLabelText("PIN nuevo"), { target: { value: "4321" } });
  fireEvent.change(screen.getByLabelText("Repetí el PIN"), { target: { value: "4321" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Restablecer PIN" }));
  });

  await expect(pendiente).resolves.toEqual({ data: { ok: true } });
  expect(svc.resetPin).toHaveBeenCalledWith("Secreta123", "4321", "4321");
  expect(svc.verifyPin).toHaveBeenCalledWith("4321");
  // La acción original, exactamente una vez.
  expect(request).toHaveBeenCalledTimes(1);
  expect(usePinGateStore.getState().isOpen).toBe(false);
});

it("una contraseña incorrecta se dice como tal y no cierra el modal", async () => {
  svc.resetPin.mockRejectedValueOnce({
    response: { status: 400, data: { detail: "La contraseña no es correcta." } },
  });
  montarModal();
  act(() => usePinGateStore.getState().openReset());
  await screen.findByText("Restablecé tu PIN");
  fireEvent.change(screen.getByLabelText("Contraseña de tu cuenta"), {
    target: { value: "mala" },
  });
  fireEvent.change(screen.getByLabelText("PIN nuevo"), { target: { value: "4321" } });
  fireEvent.change(screen.getByLabelText("Repetí el PIN"), { target: { value: "4321" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Restablecer PIN" }));
  });
  expect(await screen.findByText("La contraseña no es correcta.")).toBeInTheDocument();
  expect(svc.verifyPin).not.toHaveBeenCalled();
  expect(usePinGateStore.getState().isOpen).toBe(true);
});
