import "@testing-library/jest-dom";

import RegisterPage from "../page";
import { redirect } from "next/navigation";

jest.mock("next/navigation", () => ({ redirect: jest.fn() }));

const mockRedirect = redirect as unknown as jest.Mock;

describe("/register", () => {
  beforeEach(() => jest.clearAllMocks());

  test("redirige a /solicitar-acceso: ya no existe el alta abierta", () => {
    RegisterPage();
    expect(mockRedirect).toHaveBeenCalledWith("/solicitar-acceso");
  });

  test("la ruta sigue existiendo (links y bookmarks viejos no se rompen)", () => {
    // Si algún día se borra el archivo, `/register` pasa a 404 y todos los
    // mails y CTAs históricos dejan de funcionar. El redirect es el contrato.
    expect(typeof RegisterPage).toBe("function");
  });
});
