import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AccessRequestForm } from "../AccessRequestForm";
import { api } from "@/lib/api";

/**
 * Alta por "Continuar con Google": el visitante vuelve de Google con
 * `?prefill=<token>` y el formulario muestra el email que Google verificó.
 *
 * Las dos cosas que este archivo protege:
 *
 * 1. El prefill hace un **merge parcial**. Toca email y nombre y nada más — si
 *    el visitante llegó con `?plan=premium`, esa respuesta sobrevive. Pisar el
 *    borrador entero con `setDraft({...})` es el bug fácil de escribir y difícil
 *    de ver.
 * 2. El token viaja en el POST. Es lo que liga la solicitud a la identidad de
 *    Google; sin eso el aprobado no puede entrar con Google y nadie se entera.
 */

jest.mock("@/lib/api", () => ({ api: { post: jest.fn(), get: jest.fn() } }));

const mockPush = jest.fn();
let searchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: jest.fn() }),
  useSearchParams: () => searchParams,
}));

const mockPost = api.post as jest.MockedFunction<typeof api.post>;
const mockGet = api.get as jest.MockedFunction<typeof api.get>;

const TOKEN = "tok-google-abc";
const EMAIL = "ana@gmail.com";

function renderForm() {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AccessRequestForm />
    </QueryClientProvider>,
  );
}

type User = ReturnType<typeof userEvent.setup>;

/** Los 8 grupos de opción cerrada, con una etiqueta unívoca de cada uno. */
const GRUPOS: ReadonlyArray<RegExp> = [
  /Más de 5 años/i,
  /Solo yo/i,
  /El margen/i,
  /Prefiero no decirlo/i,
  /Excel o Google Sheets/i,
  /Entre 1 y 3 años/i,
  /Sí, y están ordenados/i,
  /Cuenta gratuita/i,
];

/** Completa todo lo que el prefill NO trae, y manda. */
async function completarYEnviar(user: User, { conNombre = false } = {}) {
  if (conNombre) {
    await user.type(screen.getByLabelText(/Nombre y apellido/i), "Ana Pérez");
  }
  await user.type(screen.getByLabelText(/Nombre del negocio/i), "Kiosco Ana");
  await user.click(screen.getByRole("button", { name: /Kiosco/i }));
  for (const etiqueta of GRUPOS) {
    await user.click(screen.getByLabelText(etiqueta));
  }
  await user.click(screen.getByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: /Pedir acceso/i }));
}

function emailInput() {
  return screen.getByLabelText(/^Email/i) as HTMLInputElement;
}

/**
 * Timeout de los tests que completan el formulario entero — mismo motivo que en
 * `access_request_form.test.tsx`: llenar 12 campos requeridos encadena una
 * docena larga de gestos de `user-event`, cada uno con su ciclo de `act()`, y
 * los 5000 ms default de Jest no alcanzan cuando los workers compiten por CPU.
 * No es lentitud del componente: con `--runInBand` pasan holgados.
 */
const TIMEOUT_FORMULARIO_COMPLETO = 20_000;

describe("AccessRequestForm — prefill de Google", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    searchParams = new URLSearchParams(`prefill=${TOKEN}`);
    mockPost.mockResolvedValue({ data: { status: "ok", message: "ok" } } as never);
  });

  test("prellena email y nombre, y deja el email de solo lectura", async () => {
    mockGet.mockResolvedValueOnce({
      data: { email: EMAIL, full_name: "Ana Pérez", provider: "google" },
    } as never);
    renderForm();

    await waitFor(() => expect(emailInput()).toHaveValue(EMAIL));
    expect(mockGet).toHaveBeenCalledWith(`/access-requests/prefill/${TOKEN}`);
    // Editarlo rompería el canje del token (403 del backend), así que no se edita.
    expect(emailInput()).toHaveAttribute("readonly");
    expect(screen.getByLabelText(/Nombre y apellido/i)).toHaveValue("Ana Pérez");
    expect(screen.getByText(/Lo verificó Google/i)).toBeInTheDocument();
  });

  test("sin claim `name` el nombre queda vacío y editable (no se inventa)", async () => {
    mockGet.mockResolvedValueOnce({
      data: { email: EMAIL, full_name: null, provider: "google" },
    } as never);
    const user = userEvent.setup({ delay: null });
    renderForm();

    await waitFor(() => expect(emailInput()).toHaveValue(EMAIL));
    const nombre = screen.getByLabelText(/Nombre y apellido/i);
    expect(nombre).toHaveValue("");
    expect(nombre).not.toHaveAttribute("readonly");

    await user.type(nombre, "Ana Pérez");
    expect(nombre).toHaveValue("Ana Pérez");
  });

  test("el prefill NO borra el plan que venía por query param", async () => {
    searchParams = new URLSearchParams(`prefill=${TOKEN}&plan=premium`);
    mockGet.mockResolvedValueOnce({
      data: { email: EMAIL, full_name: "Ana Pérez", provider: "google" },
    } as never);
    renderForm();

    await waitFor(() => expect(emailInput()).toHaveValue(EMAIL));
    // El merge es parcial: toca email y nombre, y nada más.
    expect(screen.getByLabelText(/Cuenta Premium/i)).toBeChecked();
  });

  test("el token viaja en el POST para que la solicitud quede ligada a Google", async () => {
    mockGet.mockResolvedValueOnce({
      data: { email: EMAIL, full_name: "Ana Pérez", provider: "google" },
    } as never);
    const user = userEvent.setup({ delay: null });
    renderForm();
    await waitFor(() => expect(emailInput()).toHaveValue(EMAIL));

    await completarYEnviar(user);

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const cuerpo = mockPost.mock.calls[0]![1] as Record<string, unknown>;
    expect(cuerpo.google_prefill_token).toBe(TOKEN);
    expect(cuerpo.email).toBe(EMAIL);
  }, TIMEOUT_FORMULARIO_COMPLETO);

  test("token vencido: formulario a mano y el POST no manda el token", async () => {
    mockGet.mockRejectedValueOnce(new Error("404"));
    const user = userEvent.setup({ delay: null });
    renderForm();

    // Nada prellenado y el email vuelve a ser editable.
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(emailInput()).toHaveValue("");
    expect(emailInput()).not.toHaveAttribute("readonly");

    await user.type(emailInput(), EMAIL);
    await completarYEnviar(user, { conNombre: true });

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const cuerpo = mockPost.mock.calls[0]![1] as Record<string, unknown>;
    // Mandar un token que sabemos muerto no liga nada: mejor no mandarlo.
    expect(cuerpo).not.toHaveProperty("google_prefill_token");
  }, TIMEOUT_FORMULARIO_COMPLETO);

  test("sin `?prefill` no se pide ningún prefill", async () => {
    searchParams = new URLSearchParams();
    renderForm();

    await waitFor(() => expect(emailInput()).toHaveValue(""));
    expect(mockGet).not.toHaveBeenCalled();
  });
});
