import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { LoginForm } from "../LoginForm";

/**
 * El último paso del circuito de acceso cerrado.
 *
 * Solicitud → verificación de email → aprobación del dueño → mail de decisión →
 * `/definir-password` → **acá**. El usuario acaba de escribir una contraseña dos
 * veces y ve la pantalla recargarse: sin banner no tiene ninguna confirmación
 * de que se guardó, ni instrucción de qué hacer. Los otros dos caminos que
 * terminan en este mismo login sí la tienen.
 *
 * `/definir-password` redirige con `?password_set=1`; este archivo asegura que
 * el parámetro no sea decorativo.
 */

let searchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => searchParams,
}));
jest.mock("@/services/auth.service", () => ({
  resendVerificationRequest: jest.fn(),
  getGoogleOAuthUrl: jest.fn(),
  loginRequest: jest.fn(),
}));

function renderLogin() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LoginForm />
    </QueryClientProvider>,
  );
}

describe("LoginForm — confirmación al volver de /definir-password", () => {
  beforeEach(() => {
    searchParams = new URLSearchParams();
  });

  test("con ?password_set=1 confirma que la contraseña se creó", () => {
    searchParams = new URLSearchParams("password_set=1");
    renderLogin();

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(
      "Contraseña creada. Ingresá con tu email y tu contraseña nueva.",
    );
  });

  test("dice 'creada' y no 'actualizada': para esta persona es la primera", () => {
    searchParams = new URLSearchParams("password_set=1");
    renderLogin();

    // Reusar el banner de `reset=1` habría sido la salida de una línea, y le
    // afirmaría al usuario que cambió una contraseña que nunca tuvo.
    expect(screen.queryByText(/Contraseña actualizada/i)).toBeNull();
  });

  test("sin el parámetro el login abre pelado", () => {
    renderLogin();
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("?registered=1 ya no dice nada: ese camino no existe más", () => {
    // El registro abierto murió con esta rama; nada redirige acá con
    // `registered=1`. Dejar viva la rama era prometer "¡Cuenta creada!" a quien
    // llegara con una URL vieja, cuando ninguna cuenta se creó.
    searchParams = new URLSearchParams("registered=1");
    renderLogin();

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/Cuenta creada/i)).toBeNull();
  });

  test("?reset=1 sigue intacto: es otro camino, con otro copy", () => {
    searchParams = new URLSearchParams("reset=1");
    renderLogin();

    expect(screen.getByRole("status")).toHaveTextContent(
      "Contraseña actualizada. Ingresá con tu nueva contraseña.",
    );
  });
});
