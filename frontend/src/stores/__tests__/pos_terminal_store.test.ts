import { useAuthStore } from "@/stores/authStore";
import { llevaHeaderDeTerminal, usePosTerminalStore } from "@/stores/posTerminalStore";

describe("posTerminalStore", () => {
  it("el logout del usuario NO borra la caja: la PC sigue siendo caja", () => {
    usePosTerminalStore
      .getState()
      .setTerminal({ terminalId: "c1", name: "Caja", secret: "s", tenantId: "t1" });
    try {
      useAuthStore.getState().logout();
    } catch {
      // jsdom puede quejarse de la navegación a /login; no importa acá.
    }
    expect(usePosTerminalStore.getState().terminal?.terminalId).toBe("c1");
  });

  it("habilitar de nuevo limpia la marca de inválida", () => {
    usePosTerminalStore.getState().markInvalid();
    usePosTerminalStore
      .getState()
      .setTerminal({ terminalId: "c2", name: "Otra", secret: "s2", tenantId: "t1" });
    expect(usePosTerminalStore.getState().invalid).toBe(false);
  });

  it.each([
    ["/pos/operations", true],
    ["/pos/operations/x/void", true],
    ["/products/abc/barcode", true],
    ["/products/abc", false],
    ["/products/lookup", false],
    ["/sales", false],
  ])("%s lleva header: %s", (url, esperado) => {
    expect(llevaHeaderDeTerminal(url)).toBe(esperado);
  });
});
