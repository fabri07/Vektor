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
 * "Continuar con Google" se lee como un login de un click. Con el registro
 * cerrado, un email sin cuenta vuelve de Google a un formulario largo: eso hay
 * que avisarlo ANTES del redirect, no descubrirlo al volver.
 */
describe("LoginForm — bifurcación de Google", () => {
  test("avisa que un email sin cuenta termina pidiendo acceso", () => {
    renderLogin();
    const boton = screen.getByRole("button", { name: /Continuar con Google/i });
    expect(boton).toBeInTheDocument();
    expect(
      screen.getByText(/todavía no tiene cuenta, te llevamos a pedir acceso/i),
    ).toBeInTheDocument();
  });

  test("el aviso está junto al botón, no escondido al final del formulario", () => {
    const { container } = renderLogin();
    const boton = screen.getByRole("button", { name: /Continuar con Google/i });
    const aviso = screen.getByText(/te llevamos a pedir acceso/i);
    const nodos = Array.from(container.querySelectorAll("*"));
    // El aviso es el elemento inmediatamente siguiente al botón en el DOM.
    expect(nodos.indexOf(aviso)).toBeGreaterThan(nodos.indexOf(boton));
    expect(boton.nextElementSibling).toBe(aviso);
  });
});
