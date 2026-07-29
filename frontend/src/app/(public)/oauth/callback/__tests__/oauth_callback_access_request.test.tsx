import "@testing-library/jest-dom";
import React from "react";
import { render, waitFor } from "@testing-library/react";

import OAuthCallbackPage from "../page";
import { exchangeGoogleSession } from "@/services/auth.service";

/**
 * El último camino por el que se podía acuñar una cuenta sin pasar por la
 * revisión manual era este: volver de Google con un email desconocido. Ahora el
 * backend responde `access_request_required` y esta página tiene que rutear al
 * formulario de solicitud con el token de prefill — si en cambio cayera al
 * `else` (el del login exitoso), leería `access_token` de un cuerpo que no lo
 * tiene y guardaría una sesión vacía.
 */

const mockReplace = jest.fn();
let searchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  useSearchParams: () => searchParams,
}));

const mockSetAuth = jest.fn();
jest.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({ token: null, setAuth: mockSetAuth }),
}));

jest.mock("@/services/auth.service", () => ({
  exchangeGoogleSession: jest.fn(),
  linkPendingOAuth: jest.fn(),
}));

const mockExchange = exchangeGoogleSession as jest.MockedFunction<
  typeof exchangeGoogleSession
>;

describe("OAuthCallbackPage — email sin cuenta", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    searchParams = new URLSearchParams("session_id=sess-1");
  });

  test("rutea a /solicitar-acceso con el token de prefill", async () => {
    mockExchange.mockResolvedValueOnce({
      status: "access_request_required",
      prefill_token: "tok/raro+1",
      email: "ana@gmail.com",
      full_name: "Ana Pérez",
      provider: "google",
    });

    render(<OAuthCallbackPage />);

    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/solicitar-acceso?prefill=tok%2Fraro%2B1&src=google",
      ),
    );
    // No hay sesión que guardar: no se creó ninguna cuenta.
    expect(mockSetAuth).not.toHaveBeenCalled();
  });
});
