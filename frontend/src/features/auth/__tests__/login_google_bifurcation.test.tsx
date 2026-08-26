import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { LoginForm } from "../LoginForm";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => new URLSearchParams(),
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

/**
 * Un botón de Google se lee como un login de un click. Con el registro cerrado,
 * un email sin cuenta vuelve de Google a un formulario largo: eso hay que
 * avisarlo ANTES del redirect, no descubrirlo al volver.
 *
 * El botón se busca sólo como localizador (su rótulo se acortó a "Google" en el
 * copy pass de 2026-08-18); lo que este archivo vigila es el AVISO y su
 * posición junto al botón, que es lo que sostiene la promesa.
 */
describe("LoginForm — bifurcación de Google", () => {
  test("avisa que un email sin cuenta termina pidiendo acceso", () => {
    renderLogin();
    const boton = screen.getByRole("button", { name: /Google/i });
    expect(boton).toBeInTheDocument();
    expect(
      screen.getByText(/todavía no está habilitado, vas a poder completar la solicitud de acceso/i),
    ).toBeInTheDocument();
  });

  test("el aviso está junto al botón, no escondido al final del formulario", () => {
    const { container } = renderLogin();
    const boton = screen.getByRole("button", { name: /Google/i });
    const aviso = screen.getByText(/completar la solicitud de acceso/i);
    const nodos = Array.from(container.querySelectorAll("*"));
    // El aviso es el elemento inmediatamente siguiente al botón en el DOM.
    expect(nodos.indexOf(aviso)).toBeGreaterThan(nodos.indexOf(boton));
    expect(boton.nextElementSibling).toBe(aviso);
  });
});
