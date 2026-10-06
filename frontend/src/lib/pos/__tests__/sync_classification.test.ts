import { clasificarSync, esFalloDeRed } from "@/lib/pos/syncClassification";
import type { PosErrorInfo } from "@/lib/pos/errors";

function info(status: number | null, code: string | null = null): PosErrorInfo {
  return {
    status,
    code,
    message: null,
    fields: {},
    tipo: status === null ? "transporte" : "respuesta",
    traceId: null,
  };
}

describe("clasificarSync", () => {
  it("sin respuesta es reintento, nunca rechazo", () => {
    expect(clasificarSync(info(null))).toBe("reintento");
    expect(esFalloDeRed(info(null))).toBe(true);
  });

  it("un timeout también es reintento sin gastar intentos", () => {
    const timeout: PosErrorInfo = { ...info(null), tipo: "timeout" };
    expect(clasificarSync(timeout)).toBe("reintento");
    expect(esFalloDeRed(timeout)).toBe(true);
  });

  it("un error del código de la caja va a revisión y no se llama falta de red", () => {
    const local: PosErrorInfo = { ...info(null), tipo: "local" };
    expect(clasificarSync(local)).toBe("revision");
    expect(esFalloDeRed(local)).toBe(false);
  });

  it("la suscripción vencida y el mantenimiento se reintentan", () => {
    // `isDefinitiveRejection` los trata como rechazo definitivo porque mira el
    // rango 4xx. Para la cola eso sería tirar una venta cobrada por algo que se
    // arregla solo cuando el dueño paga o el mantenimiento termina.
    expect(clasificarSync(info(402, "SUBSCRIPTION_READ_ONLY"))).toBe("reintento");
    expect(clasificarSync(info(423))).toBe("reintento");
    expect(clasificarSync(info(503, "SUBSCRIPTION_UNAVAILABLE"))).toBe("reintento");
  });

  it("los transitorios del servidor se reintentan", () => {
    for (const s of [408, 429, 500, 502, 504]) {
      expect(clasificarSync(info(s))).toBe("reintento");
    }
  });

  it("la sesión y la caja dada de baja piden volver a entrar", () => {
    expect(clasificarSync(info(401))).toBe("autenticacion");
    expect(clasificarSync(info(403, "TERMINAL_DISABLED"))).toBe("autenticacion");
    expect(clasificarSync(info(403, "TERMINAL_UNKNOWN"))).toBe("autenticacion");
    expect(clasificarSync(info(403, "TERMINAL_REQUIRED_FOR_CAPTURE"))).toBe("autenticacion");
  });

  it("un permiso revocado lo mira una persona, no se reintenta solo", () => {
    expect(clasificarSync(info(403, "POS_PERMISSION_DENIED"))).toBe("revision");
  });

  it("lo que el servidor rechazó de verdad va a revisión", () => {
    expect(clasificarSync(info(400, "INSUFFICIENT_STOCK"))).toBe("revision");
    expect(clasificarSync(info(422, "PRICE_TOKEN_INVALID"))).toBe("revision");
    expect(clasificarSync(info(422, "PRICE_TOKEN_EXPIRED"))).toBe("revision");
    expect(clasificarSync(info(409, "IDEMPOTENCY_KEY_REUSED"))).toBe("revision");
  });

  it("un 2xx es sincronizada", () => {
    expect(clasificarSync(info(200))).toBe("sincronizada");
    expect(clasificarSync(info(201))).toBe("sincronizada");
  });
});
