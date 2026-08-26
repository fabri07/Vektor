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
 * `/login` es la página de auth más transitada y era el último lugar donde
 * sobrevivía el vocabulario del registro abierto.
 */
describe("LoginForm — CTA de alta", () => {
  test("manda a pedir acceso, no a crear una cuenta gratis", () => {
    renderLogin();
    const cta = screen.getByRole("link", { name: /Solicitá tu acceso/i });
    expect(cta).toHaveAttribute("href", "/solicitar-acceso?src=login");
  });

  test("no promete un alta gratuita ni apunta a /register", () => {
    const { container } = renderLogin();
    expect(screen.queryByText(/Creá una gratis/i)).toBeNull();
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) =>
      a.getAttribute("href"),
    );
    expect(hrefs).not.toContain("/register");
  });
});
