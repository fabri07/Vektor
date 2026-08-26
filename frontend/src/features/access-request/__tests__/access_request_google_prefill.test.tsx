import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AccessRequestForm } from "../AccessRequestForm";
import { api } from "@/lib/api";
import {
  CAN_SHARE_FILES_OPTIONS,
  HISTORY_DEPTH_OPTIONS,
  MAIN_CONCERN_OPTIONS,
  RECORDS_FORMAT_OPTIONS,
  REQUESTED_PLAN_OPTIONS,
  REVENUE_BAND_OPTIONS,
  STAFF_SIZE_OPTIONS,
  YEARS_OPERATING_OPTIONS,
  labelOf,
} from "@/lib/accessRequestOptions";

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

/**
 * Los 8 grupos de opción cerrada, con una opción unívoca de cada uno. Los
 * rótulos salen del catálogo compartido: acá son LOCALIZADORES para completar
 * el formulario y llegar al envío, no contenido que este archivo verifique
 * (mismo criterio que `access_request_form.test.tsx`).
 */
const GRUPOS: ReadonlyArray<string> = [
  labelOf(YEARS_OPERATING_OPTIONS, "gt_5y"),
  labelOf(STAFF_SIZE_OPTIONS, "solo"),
  labelOf(MAIN_CONCERN_OPTIONS, "MARGIN"),
  labelOf(REVENUE_BAND_OPTIONS, "no_contesta"),
  labelOf(RECORDS_FORMAT_OPTIONS, "planilla"),
  labelOf(HISTORY_DEPTH_OPTIONS, "1y_3y"),
  labelOf(CAN_SHARE_FILES_OPTIONS, "si_ordenados"),
  labelOf(REQUESTED_PLAN_OPTIONS, "free"),
];

/** Completa todo lo que el prefill NO trae, y manda. */
async function completarYEnviar(user: User, { conNombre = false } = {}) {
  if (conNombre) {
    await user.type(screen.getByLabelText(/Nombre y apellido/i), "Ana Pérez");
  }
  await user.type(screen.getByLabelText(/Nombre del negocio/i), "Kiosco Ana");
  await user.click(screen.getByRole("radio", { name: /Kiosco/i }));
  for (const etiqueta of GRUPOS) {
    await user.click(screen.getByLabelText(etiqueta, { exact: false }));
  }
  await user.click(screen.getByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: /Enviar mi solicitud/i }));
}

function emailInput() {
  return screen.getByLabelText(/^Email/i) as HTMLInputElement;
}

/**
 * Timeout de los tests que completan el formulario entero — mismo motivo que en
 * `access_request_form.test.tsx`: llenar 13 campos requeridos encadena una
 * docena larga de gestos de `user-event`, cada uno con su ciclo de `act()`, y
 * los 5000 ms default de Jest no alcanzan cuando los workers compiten por CPU.
 * No es lentitud del componente: con `--runInBand` pasan holgados.
 */
const TIMEOUT_FORMULARIO_COMPLETO = 20_000;

describe("AccessRequestForm — prefill de Google", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // `clearAllMocks` NO borra implementaciones: un `mockResolvedValue` puesto
    // en un test se filtra a los siguientes y los vuelve dependientes del
    // orden. Estos tests distinguen "el prefill responde" de "el prefill se
    // cayó", así que arrancar de cero no es opcional.
    mockGet.mockReset();
    // Mismo razonamiento, para el borrador: el formulario lo persiste en
    // `sessionStorage`, jsdom lo comparte entre tests, y acá importa el doble
    // — un nombre sobreviviente haría pasar en verde el test de "sin claim
    // `name` no se inventa nada".
    window.sessionStorage.clear();
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
    expect(screen.getByLabelText(labelOf(REQUESTED_PLAN_OPTIONS, "premium"), { exact: false })).toBeChecked();
  });

  test("el token viaja en el POST para que la solicitud quede ligada a Google", async () => {
    // `mockResolvedValue` y no `...Once`: el envío revalida el prefill, así que
    // hay dos lecturas — la del montaje y la del submit.
    mockGet.mockResolvedValue({
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
    // La revalidación es una lectura del mismo endpoint (el backend hace GET,
    // no GETDEL): no consume el token, así que el POST lo sigue pudiendo canjear.
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(mockGet).toHaveBeenNthCalledWith(2, `/access-requests/prefill/${TOKEN}`);
  }, TIMEOUT_FORMULARIO_COMPLETO);

  /**
   * El prefill vence mientras se completa el formulario.
   *
   * Es el caso que se perdía en silencio: el backend persiste la solicitud sin
   * `google_subject` (hace bien, no inventa un subject ni tira 16 respuestas),
   * pero al aprobarla no se crea la identidad de Google y el usuario termina
   * obligado a definir contraseña — justo la fricción que "Continuar con
   * Google" evitaba. La UI seguía diciendo "Lo verificó Google".
   */
  describe("el prefill se vence entre el montaje y el envío", () => {
    /** Prefill OK al montar, muerto al revalidar en el submit. */
    function prefillQueVence() {
      mockGet
        .mockResolvedValueOnce({
          data: { email: EMAIL, full_name: "Ana Pérez", provider: "google" },
        } as never)
        .mockRejectedValueOnce(new Error("404"));
    }

    test("el primer envío NO manda: avisa qué cambió y degrada el email a editable", async () => {
      prefillQueVence();
      const user = userEvent.setup({ delay: null });
      renderForm();
      await waitFor(() => expect(emailInput()).toHaveValue(EMAIL));
      expect(screen.getByText(/Lo verificó Google/i)).toBeInTheDocument();

      await completarYEnviar(user);

      // Nada se mandó: el aviso tiene que llegar a leerse, y navegando a
      // /solicitud-enviada en el mismo gesto no se leería.
      expect(mockPost).not.toHaveBeenCalled();
      expect(mockPush).not.toHaveBeenCalled();

      // Y se dice la verdad: ya no lo verifica Google, y se explica cómo va a entrar.
      expect(screen.queryByText(/Lo verificó Google/i)).toBeNull();
      expect(emailInput()).not.toHaveAttribute("readonly");
      const aviso = await screen.findByText(/ese vínculo ya no vale/i);
      expect(aviso).toHaveTextContent(/No se pierde nada de lo que contestaste/i);
      expect(aviso).toHaveTextContent(/definiendo una contraseña/i);
    }, TIMEOUT_FORMULARIO_COMPLETO);

    test("el segundo envío manda la solicitud, sin el token muerto", async () => {
      prefillQueVence();
      const user = userEvent.setup({ delay: null });
      renderForm();
      await waitFor(() => expect(emailInput()).toHaveValue(EMAIL));

      await completarYEnviar(user);
      await screen.findByText(/ese vínculo ya no vale/i);

      // El visitante leyó el aviso y aprieta de nuevo: las 16 respuestas siguen ahí.
      await user.click(screen.getByRole("button", { name: /Enviar mi solicitud/i }));

      await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
      const cuerpo = mockPost.mock.calls[0]![1] as Record<string, unknown>;
      // Mandar un token que sabemos muerto no liga nada.
      expect(cuerpo).not.toHaveProperty("google_prefill_token");
      expect(cuerpo.email).toBe(EMAIL);
      // Y no se revalida de nuevo: ya no hay token que revalidar.
      expect(mockGet).toHaveBeenCalledTimes(2);
    }, TIMEOUT_FORMULARIO_COMPLETO);
  });

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
