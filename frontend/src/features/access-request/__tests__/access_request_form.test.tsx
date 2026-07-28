import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AccessRequestForm } from "../AccessRequestForm";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({ api: { post: jest.fn() } }));

const mockPush = jest.fn();
let searchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: jest.fn() }),
  useSearchParams: () => searchParams,
}));

const mockPost = api.post as jest.MockedFunction<typeof api.post>;

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

/** Completa el bloque de contacto. */
async function fillContacto(user: User) {
  await user.type(screen.getByLabelText(/Nombre y apellido/i), "Ana Pérez");
  await user.type(screen.getByLabelText(/^Email/i), "ana@negocio.com");
  await user.type(screen.getByLabelText(/Nombre del negocio/i), "Kiosco Ana");
}

/** Completa el screening (tu negocio + tu info) y el plan. */
async function fillScreening(user: User) {
  await user.click(screen.getByLabelText(/Más de 5 años/i));
  await user.click(screen.getByLabelText(/Solo yo/i));
  await user.click(screen.getByLabelText(/El margen/i));
  await user.click(screen.getByLabelText(/Prefiero no decirlo/i));
  await user.click(screen.getByLabelText(/Excel o Google Sheets/i));
  await user.click(screen.getByLabelText(/Entre 1 y 3 años/i));
  await user.click(screen.getByLabelText(/Sí, y están ordenados/i));
  await user.click(screen.getByLabelText(/Cuenta gratuita/i));
}

function submitButton() {
  return screen.getByRole("button", { name: /Pedir acceso/i });
}

describe("AccessRequestForm", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    searchParams = new URLSearchParams();
  });

  test("renderiza las secciones del formulario y el aviso de confidencialidad", () => {
    renderForm();
    expect(screen.getByText("Contacto")).toBeInTheDocument();
    expect(screen.getByText("Rubro")).toBeInTheDocument();
    expect(screen.getByText("Tu negocio")).toBeInTheDocument();
    expect(screen.getByText("Tu info")).toBeInTheDocument();
    expect(screen.getByText("Cómo querés usar Véktor")).toBeInTheDocument();
    expect(screen.getByText(/Esta información es confidencial/)).toBeInTheDocument();
    expect(
      screen.getByText(/no reporta a ARCA \(ex-AFIP\) ni comparte tu información/),
    ).toBeInTheDocument();
  });

  test("NO existe ningún campo de contraseña: este formulario no crea la cuenta", () => {
    const { container } = renderForm();
    expect(container.querySelectorAll('input[type="password"]')).toHaveLength(0);
  });

  test("el honeypot está presente pero oculto", () => {
    const { container } = renderForm();
    const honeypot = container.querySelector<HTMLInputElement>('input[name="website"]');
    expect(honeypot).not.toBeNull();
    expect(honeypot).toHaveClass("hidden");
    expect(honeypot).toHaveAttribute("tabindex", "-1");
  });

  test("elegir 'Otro' revela el textarea y sin texto el submit sigue bloqueado", async () => {
    const user = userEvent.setup();
    renderForm();

    expect(screen.queryByLabelText(/Contanos de qué es tu negocio/i)).toBeNull();

    await fillContacto(user);
    await fillScreening(user);
    await user.click(screen.getByRole("button", { name: /Otro/i }));
    await user.click(screen.getByRole("checkbox"));

    const textarea = screen.getByLabelText(/Contanos de qué es tu negocio/i);
    expect(textarea).toBeInTheDocument();
    // "Otros" sin descripción: el backend lo rechazaría (validación no-vacío).
    expect(submitButton()).toBeDisabled();

    // Espacios en blanco tampoco alcanzan: se recortan antes de medir.
    await user.type(textarea, "   ");
    expect(submitButton()).toBeDisabled();

    await user.type(textarea, "Ferretería de barrio");
    await waitFor(() => expect(submitButton()).toBeEnabled());
  });

  test("el submit está bloqueado hasta que el formulario es válido", async () => {
    const user = userEvent.setup();
    renderForm();

    expect(submitButton()).toBeDisabled();

    await fillContacto(user);
    expect(submitButton()).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    expect(submitButton()).toBeDisabled();

    await fillScreening(user);
    // Falta el consentimiento: es obligatorio, no decorativo.
    expect(submitButton()).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(submitButton()).toBeEnabled());
  });

  test("envío exitoso: postea el contrato exacto (con consent, sin password)", async () => {
    mockPost.mockResolvedValueOnce({ data: { status: "ok", message: "ok" } });
    const user = userEvent.setup();
    renderForm();

    await fillContacto(user);
    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    await fillScreening(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(submitButton());

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, payload] = mockPost.mock.calls[0]!;
    expect(url).toBe("/access-requests");

    const cuerpo = payload as Record<string, unknown>;
    expect(cuerpo).toMatchObject({
      full_name: "Ana Pérez",
      email: "ana@negocio.com",
      business_name: "Kiosco Ana",
      requested_vertical: "kiosco_almacen",
      requested_plan: "free",
      years_operating: "gt_5y",
      staff_size: "solo",
      main_concern: "MARGIN",
      monthly_revenue_band: "no_contesta",
      records_format: "planilla",
      history_depth: "1y_3y",
      can_share_files: "si_ordenados",
      consent: true,
      website: "",
    });
    // Sin rubro "otros" el texto libre viaja null: mandarlo sería 422.
    expect(cuerpo.vertical_other_text).toBeNull();
    // El endpoint usa extra="forbid": un campo de más rompe el alta.
    expect(cuerpo).not.toHaveProperty("password");
    expect(typeof cuerpo.elapsed_ms).toBe("number");

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith(
        "/solicitud-enviada?email=ana%40negocio.com",
      ),
    );
  });

  test("?plan=premium precarga el plan y lo deja editable", async () => {
    searchParams = new URLSearchParams("plan=premium");
    const user = userEvent.setup();
    renderForm();

    await waitFor(() =>
      expect(screen.getByLabelText(/Cuenta Premium/i)).toBeChecked(),
    );

    await user.click(screen.getByLabelText(/Cuenta gratuita/i));
    expect(screen.getByLabelText(/Cuenta gratuita/i)).toBeChecked();
    expect(screen.getByLabelText(/Cuenta Premium/i)).not.toBeChecked();
  });

  test("un error del backend muestra el mensaje y no navega", async () => {
    mockPost.mockRejectedValueOnce(new Error("network"));
    const user = userEvent.setup();
    renderForm();

    await fillContacto(user);
    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    await fillScreening(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(submitButton());

    expect(await screen.findByText(/No pudimos enviar tu solicitud/i)).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  });
});
