import {
  type PosProduct,
  type SaleState,
  buildPayload,
  cannotCharge,
  initialSale,
  isDefinitiveRejection,
  lineGrossCents,
  localNaiveIso,
  previewTotals,
  saleReducer,
  toCents,
} from "@/lib/pos/cart";

const yerba: PosProduct = {
  id: "p1",
  name: "Yerba",
  sale_price_ars: "1000.00",
  sale_unit: "unit",
  base_units_per_sale_unit: 1,
  stock_units: 10,
};

function con(...acciones: Parameters<typeof saleReducer>[1][]): SaleState {
  return acciones.reduce(saleReducer, initialSale);
}

describe("montos", () => {
  it("toCents lee coma y punto, y rechaza lo que no es monto", () => {
    expect(toCents("1234.56")).toBe(123456);
    expect(toCents("1234,5")).toBe(123450);
    expect(toCents("12")).toBe(1200);
    expect(toCents("1.234,56")).toBeNull();
    expect(toCents("-5")).toBeNull();
  });

  it("$1.234,56 el kilo × 750 g = $925,92 (espejo del servidor)", () => {
    expect(lineGrossCents(123456, 750, 1000)).toBe(92592);
  });

  it("redondea half-up una vez por línea, como el servidor", () => {
    // $0,01 × 500 g / 1000 = 0,5 centavo → 1.
    expect(lineGrossCents(1, 500, 1000)).toBe(1);
    expect(lineGrossCents(1, 499, 1000)).toBe(0);
  });
});

describe("carrito", () => {
  it("escanear dos veces el mismo producto suma cantidad", () => {
    const s = con({ type: "ADD_PRODUCT", product: yerba }, { type: "ADD_PRODUCT", product: yerba });
    expect(s.lines).toHaveLength(1);
    expect(s.lines[0]?.quantity).toBe(2);
  });

  it("total = líneas − descuentos de línea − descuento global", () => {
    const s = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SET_QUANTITY", productId: "p1", quantity: 3 },
      { type: "SET_LINE_DISCOUNT", productId: "p1", cents: 5000 },
      { type: "SET_GLOBAL_DISCOUNT", cents: 100 },
    );
    expect(previewTotals(s)).toEqual({ subtotalCents: 295000, totalCents: 294900 });
  });

  it("no deja cobrar en $0, sin cliente fiado ni con efectivo insuficiente", () => {
    const base = con({ type: "ADD_PRODUCT", product: yerba });
    expect(cannotCharge(saleReducer(base, { type: "SET_GLOBAL_DISCOUNT", cents: 100000 }))).toMatch(
      /\$0/,
    );
    expect(cannotCharge(saleReducer(base, { type: "SET_PAYMENT", method: "account" }))).toMatch(
      /cliente/,
    );
    expect(cannotCharge(saleReducer(base, { type: "SET_CASH_RECEIVED", value: "999" }))).toMatch(
      /no alcanza/,
    );
    expect(cannotCharge(base)).toBeNull();
  });
});

describe("estados del cobro", () => {
  const payload = buildPayload(con({ type: "ADD_PRODUCT", product: yerba }), "id-1", new Date());

  it("en duda el carrito queda bloqueado: no se agrega ni se edita", () => {
    const enDuda = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SEND", payload },
      { type: "UNKNOWN", notice: "x" },
    );
    expect(enDuda.status).toBe("en_duda");
    const tocado = [
      { type: "ADD_PRODUCT", product: yerba } as const,
      { type: "SET_QUANTITY", productId: "p1", quantity: 9 } as const,
      { type: "REMOVE", productId: "p1" } as const,
    ].reduce(saleReducer, enDuda);
    expect(tocado).toBe(enDuda);
  });

  it("el reintento conserva el payload congelado", () => {
    const enDuda = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SEND", payload },
      { type: "UNKNOWN", notice: "x" },
    );
    expect(enDuda.payload).toBe(payload);
  });

  it("un rechazo definitivo desbloquea y descarta el payload (el próximo nace con id nuevo)", () => {
    const rechazada = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SEND", payload },
      { type: "REJECTED", notice: "sin stock" },
    );
    expect(rechazada.status).toBe("editando");
    expect(rechazada.payload).toBeNull();
  });

  it("escanear después de cobrar arranca la venta siguiente", () => {
    const cobrada = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SEND", payload },
      {
        type: "SUCCESS",
        result: {
          id: "o",
          client_operation_id: "id-1",
          total_ars: "1000.00",
          subtotal_ars: "1000.00",
          discount_ars: "0.00",
          cash_received_ars: null,
          cash_change_ars: null,
          status: "COMPLETED",
        },
      },
    );
    const siguiente = saleReducer(cobrada, { type: "ADD_PRODUCT", product: yerba });
    expect(siguiente.status).toBe("editando");
    expect(siguiente.lines[0]?.quantity).toBe(1);
    expect(siguiente.result).toBeNull();
  });
});

describe("clasificación de la respuesta", () => {
  it.each([
    [null, null, false],
    [500, null, false],
    [503, null, false],
    [408, null, false],
    [429, null, false],
    [409, "IDEMPOTENCY_KEY_REUSED", false],
    [409, "DUPLICATE_IDEMPOTENT", false],
    [400, "INSUFFICIENT_STOCK", true],
    [422, "TENDERS_MISMATCH", true],
    [403, "TERMINAL_REQUIRED", true],
    [402, null, true],
  ])("status %s código %s → definitivo: %s", (status, code, esperado) => {
    expect(isDefinitiveRejection({ status }, code)).toBe(esperado);
  });
});

describe("payload", () => {
  it("la fecha va en hora local, sin zona", () => {
    expect(localNaiveIso(new Date(2026, 8, 25, 23, 50, 7))).toBe("2026-09-25T23:50:07");
  });

  it("fiado lleva el cliente; efectivo lleva lo recibido; el pago es el total", () => {
    const fiado = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SET_PAYMENT", method: "account" },
      { type: "SET_CUSTOMER", id: "c1", name: "Ana" },
    );
    const p = buildPayload(fiado, "id", new Date());
    expect(p.customer_id).toBe("c1");
    expect(p.cash_received_ars).toBeNull();
    expect(p.tenders).toEqual([{ payment_method: "account", amount_ars: "1000.00" }]);

    const efectivo = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SET_CASH_RECEIVED", value: "2000" },
    );
    expect(buildPayload(efectivo, "id", new Date()).cash_received_ars).toBe("2000.00");
  });
});
