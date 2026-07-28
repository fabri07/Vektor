import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError } from "axios";

import ContactoPage from "../page";
import { api } from "@/lib/api";
import { CONSENT_VERSION } from "@/lib/contact";

jest.mock("@/lib/api", () => ({ api: { post: jest.fn() } }));

const mockPost = api.post as jest.MockedFunction<typeof api.post>;

interface DataLayerWindow extends Window {
  dataLayer?: Array<Record<string, unknown>>;
}

function dataLayer(): Array<Record<string, unknown>> {
  return (window as DataLayerWindow).dataLayer ?? [];
}

/** Completa todos los campos requeridos del formulario. */
async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/Tu nombre/i), "Ana Pérez");
  await user.clear(screen.getByLabelText(/Tu celular/i));
  await user.type(screen.getByLabelText(/Tu celular/i), "+54 11 5555 5555");
  await user.type(screen.getByLabelText(/Correo corporativo/i), "ana@negocio.com");
  await user.type(screen.getByLabelText(/^Empresa/i), "Kiosco Ana");
  await user.selectOptions(screen.getByLabelText(/Rubro/i), "kiosco_almacen");
  await user.click(screen.getByLabelText("1 a 5"));
}

describe("ContactoPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (window as DataLayerWindow).dataLayer = [];
  });

  test("muestra el título literal solicitado y registra form_view", () => {
    render(<ContactoPage />);
    expect(
      screen.getByText("Dejá tus datos y te contactaremos para conversar")
    ).toBeInTheDocument();
    expect(dataLayer().some((e) => e.event === "contact_form_view")).toBe(true);
  });

  test("el select de rubro usa el vocabulario de /contact/leads, no el de solicitudes", () => {
    render(<ContactoPage />);
    const select = screen.getByLabelText(/Rubro/i) as HTMLSelectElement;
    const valores = Array.from(select.options)
      .map((o) => o.value)
      .filter(Boolean);
    // Los tres rubros reales vienen de lib/verticals.ts...
    expect(valores).toEqual(
      expect.arrayContaining(["kiosco_almacen", "limpieza", "decoracion_hogar"]),
    );
    // ...pero el "Otro" es el de `LeadRubro` ("otro"), NO el `RequestedVertical`
    // ("otros"): son endpoints distintos y mezclarlos es un 422 por lead.
    expect(valores).toContain("otro");
    expect(valores).not.toContain("otros");
  });

  test("el honeypot está presente pero oculto", () => {
    const { container } = render(<ContactoPage />);
    const honeypot = container.querySelector<HTMLInputElement>('input[name="website"]');
    expect(honeypot).not.toBeNull();
    expect(honeypot).toHaveClass("hidden");
    expect(honeypot).toHaveAttribute("tabindex", "-1");
  });

  test("sin consentimiento no envía: el checkbox es required y el guard corta", async () => {
    const user = userEvent.setup();
    const { container } = render(<ContactoPage />);
    await fillRequired(user);
    // Enforcement nativo: el checkbox es obligatorio.
    expect(screen.getByRole("checkbox")).toBeRequired();
    // Guard JS defensivo: disparar submit directo (evita el gate nativo) sin
    // consentimiento muestra el error y NO postea.
    fireEvent.submit(container.querySelector("form")!);
    expect(
      await screen.findByText(/Necesitás aceptar la política de privacidad/i)
    ).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test("envío exitoso: postea con consent_version + cta_source y muestra gracias", async () => {
    mockPost.mockResolvedValueOnce({ data: { status: "ok" } });
    const user = userEvent.setup();
    render(<ContactoPage />);
    await fillRequired(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Enviar/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, payload] = mockPost.mock.calls[0]!;
    expect(url).toBe("/contact/leads");
    expect(payload).toMatchObject({
      consent: true,
      consent_version: CONSENT_VERSION,
      cta_source: "contacto_page",
    });
    expect(await screen.findByText(/¡Gracias!/i)).toBeInTheDocument();
    expect(dataLayer().some((e) => e.event === "contact_form_submit_success")).toBe(true);
  });

  test("error 422 muestra mensaje de datos y registra submit_error", async () => {
    mockPost.mockRejectedValueOnce(
      new AxiosError("bad", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 422,
      } as never)
    );
    const user = userEvent.setup();
    render(<ContactoPage />);
    await fillRequired(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Enviar/i }));

    expect(
      await screen.findByText(/Revisá los datos.*teléfono/i)
    ).toBeInTheDocument();
    expect(dataLayer().some((e) => e.event === "contact_form_submit_error")).toBe(true);
  });

  test("error genérico muestra mensaje de reintento", async () => {
    mockPost.mockRejectedValueOnce(new Error("network"));
    const user = userEvent.setup();
    render(<ContactoPage />);
    await fillRequired(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Enviar/i }));

    expect(
      await screen.findByText(/No pudimos enviar el formulario/i)
    ).toBeInTheDocument();
  });

  test("doble submit no dispara dos requests", async () => {
    // Promesa que no resuelve: el form queda en 'sending' y el guard bloquea el 2º.
    mockPost.mockReturnValueOnce(new Promise(() => {}) as never);
    const user = userEvent.setup();
    render(<ContactoPage />);
    await fillRequired(user);
    await user.click(screen.getByRole("checkbox"));
    const submit = screen.getByRole("button", { name: /Enviar/i });
    await user.click(submit);
    await user.click(submit);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });
});
