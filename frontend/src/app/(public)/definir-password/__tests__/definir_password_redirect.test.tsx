import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import DefinirPasswordPage from "../page";
import { resetPasswordRequest } from "@/services/auth.service";

/**
 * La otra mitad de `login_password_set_banner.test.tsx`.
 *
 * Ahí se verifica que `/login` sepa leer `?password_set=1`; acá, que esta
 * página lo mande. Los dos extremos separados pasaban en verde mientras el
 * circuito terminaba en silencio: la página redirigía con un parámetro que
 * nadie interpretaba.
 */

const mockReplace = jest.fn();
let searchParams = new URLSearchParams("token=tok-de-invitacion");

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: mockReplace }),
  useSearchParams: () => searchParams,
}));
jest.mock("@/services/auth.service", () => ({
  resetPasswordRequest: jest.fn(),
}));

const mockReset = resetPasswordRequest as jest.MockedFunction<
  typeof resetPasswordRequest
>;

describe("/definir-password", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    searchParams = new URLSearchParams("token=tok-de-invitacion");
  });

  test("al crear la contraseña vuelve al login pidiendo la confirmación", async () => {
    mockReset.mockResolvedValue(undefined as never);
    const user = userEvent.setup({ delay: null });
    render(<DefinirPasswordPage />);

    await user.type(screen.getByLabelText("Tu contraseña"), "vektor123");
    await user.type(screen.getByLabelText("Repetila"), "vektor123");
    await user.click(screen.getByRole("button", { name: /Crear mi contraseña/i }));

    await waitFor(() =>
      expect(mockReset).toHaveBeenCalledWith("tok-de-invitacion", "vektor123"),
    );
    // El parámetro exacto que `LoginForm` interpreta. Si acá se cambia por
    // `reset=1` o por nada, el usuario termina el circuito sin confirmación.
    expect(mockReplace).toHaveBeenCalledWith("/login?password_set=1");
  });

  test("si el token es inválido no redirige: se queda y lo dice", async () => {
    mockReset.mockRejectedValue(new Error("410"));
    const user = userEvent.setup({ delay: null });
    render(<DefinirPasswordPage />);

    await user.type(screen.getByLabelText("Tu contraseña"), "vektor123");
    await user.type(screen.getByLabelText("Repetila"), "vektor123");
    await user.click(screen.getByRole("button", { name: /Crear mi contraseña/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/El link es inválido o venció/i);
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
