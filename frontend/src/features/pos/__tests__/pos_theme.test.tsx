/**
 * Tema claro/oscuro de la caja, y la guarda que evita volver a tener texto
 * invisible: el bug de la primera prueba fue un color fijo claro/oscuro en una
 * pantalla cuyo fondo depende del contexto.
 */
import fs from "fs";
import path from "path";

import { act, fireEvent, render, screen } from "@testing-library/react";

import { PosScreen } from "@/features/pos/PosScreen";
import { useAuthStore } from "@/stores/authStore";
import { usePosPrintConfigStore } from "@/stores/posPrintConfigStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";
import { usePosThemeStore } from "@/stores/posThemeStore";

jest.mock("@/services/pos.service", () => ({
  posService: { listOperations: jest.fn().mockResolvedValue([]) },
}));
jest.mock("@/services/auth.service", () => ({ logoutRequest: jest.fn() }));

const RAIZ = path.join(__dirname, "..", "..", "..", "..");
const leer = (rel: string) => fs.readFileSync(path.join(RAIZ, rel), "utf8");

beforeEach(() => {
  window.localStorage.clear();
  usePosThemeStore.setState({ theme: "claro" });
  usePosPrintConfigStore.setState({ autoPrint: false });
  usePosTerminalStore.setState({ terminal: null, invalid: false });
});

describe("tema de la caja", () => {
  it("arranca claro y el botón lo pasa a oscuro y vuelve", async () => {
    useAuthStore.setState({
      token: "t",
      user: { id: "u", email: "a@b.com", full_name: "Ana", role: "OWNER", tenant_id: "t1" },
    });
    render(<PosScreen />);
    expect(screen.getByTestId("pos-root")).toHaveClass("pos-claro");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Pasar a tema oscuro" }));
    });
    expect(screen.getByTestId("pos-root")).toHaveClass("pos-oscuro");
    // Se recuerda en la PC.
    expect(window.localStorage.getItem("vektor-pos-tema")).toContain("oscuro");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Pasar a tema claro" }));
    });
    expect(screen.getByTestId("pos-root")).toHaveClass("pos-claro");
  });

  it("el bloqueo de PC no habilitada también lleva el tema", () => {
    usePosThemeStore.setState({ theme: "oscuro" });
    useAuthStore.setState({
      token: "t",
      user: { id: "u", email: "a@b.com", full_name: "Ana", role: "CASHIER", tenant_id: "t1" },
    });
    render(<PosScreen />);
    expect(screen.getByText(/no está habilitada como caja/)).toBeInTheDocument();
    expect(screen.getByTestId("pos-root")).toHaveClass("pos-oscuro");
  });
});

describe("guarda de colores", () => {
  // Las pantallas de caja usan SÓLO los tokens `pos-*` para fondo, texto y
  // borde. Los modales (panel oscuro fijo) y el ticket (su propio CSS) quedan
  // afuera a propósito.
  const PANTALLAS = [
    "PosScreen",
    "CartTable",
    "PaymentPanel",
    "DiscountControl",
    "ScanInput",
    "VoidLastTicket",
    "RecentTickets",
    "PendingOperationsPanel",
    "CashierHome",
  ];
  const FIJOS =
    /\b(?:bg|text|border|divide)-(?:vk-(?:text-(?:primary|secondary|muted)|surface-w|bg-light|border-w)|vektor-(?:white|body|muted|surface|ink|night|border))\b/g;

  it.each(PANTALLAS)("%s no usa colores fijos de fondo/texto/borde", (nombre) => {
    const fuente = leer(`src/features/pos/${nombre}.tsx`);
    expect(fuente.match(FIJOS) ?? []).toEqual([]);
  });

  it("cada token pos-* tiene su variable en los dos temas", () => {
    const tailwind = leer("tailwind.config.ts");
    const css = leer("src/styles/globals.css");
    const variables = [...tailwind.matchAll(/var\(--(pos-[a-z0-9-]+)\)/g)].map((m) => m[1]);
    expect(variables.length).toBeGreaterThan(5);
    const bloque = (selector: string) => {
      const i = css.indexOf(selector);
      return css.slice(i, css.indexOf("}", i));
    };
    const claro = bloque(".pos-claro {\n    --pos");
    const oscuro = bloque(".pos-oscuro {");
    for (const v of variables) {
      expect(claro).toContain(`--${v}:`);
      expect(oscuro).toContain(`--${v}:`);
    }
  });
});
