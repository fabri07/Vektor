import {
  type PosProduct,
  type SaleState,
  buildPayload,
  cannotCharge,
  cashPartCents,
  unassignedCents,
  initialSale,
  esCorregibleEnElMostrador,
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
    expect(
      cannotCharge(saleReducer(base, { type: "SET_PAYMENT_METHOD", id: "p0", method: "account" })),
    ).toMatch(
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

  it("mientras se envía, el carrito no se toca", () => {
    const enviando = con({ type: "ADD_PRODUCT", product: yerba }, { type: "SEND", payload });
    expect(enviando.status).toBe("enviando");
    const tocado = [
      { type: "ADD_PRODUCT", product: yerba } as const,
      { type: "SET_QUANTITY", productId: "p1", quantity: 9 } as const,
      { type: "REMOVE", productId: "p1" } as const,
    ].reduce(saleReducer, enviando);
    expect(tocado).toBe(enviando);
  });

  it("sin respuesta la venta va a la cola y el carrito se LIBERA", () => {
    // Antes de B7 quedaba "en duda" y la caja se trababa hasta que el cajero
    // resolviera el cobro a mano — justo cuando más falta hace vender.
    const encolada = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SEND", payload },
      { type: "QUEUED", notice: "se sube sola" },
    );
    expect(encolada.status).toBe("editando");
    expect(encolada.lines).toEqual([]);
    expect(encolada.payload).toBeNull();
    expect(encolada.notice).toBe("se sube sola");
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
  // `true` = el cajero puede corregir el carrito y volver a cobrar, y sólo por
  // eso se borra la venta local. Todo lo demás queda encolado: el cajero ya
  // recibió la plata.
  it.each([
    [null, null, false],
    [500, null, false],
    [503, null, false],
    [408, null, false],
    [429, null, false],
    [409, "IDEMPOTENCY_KEY_REUSED", false],
    [409, "DUPLICATE_IDEMPOTENT", false],
    // Los dos que se arreglan solos sin tocar la venta. Antes daban `true` y
    // la venta cobrada desaparecía de los dos lados.
    [402, null, false],
    [423, null, false],
    // La sesión o la PC, no la venta: tampoco hay nada que corregir acá.
    [401, null, false],
    [403, "TERMINAL_REQUIRED", false],
    [403, "TERMINAL_DISABLED", false],
    // Éstos sí: el cajero saca el producto, corrige el pago y vuelve a cobrar.
    [400, "INSUFFICIENT_STOCK", true],
    [422, "TENDERS_MISMATCH", true],
    [422, "PRICE_TOKEN_INVALID", true],
    [403, "POS_PERMISSION_DENIED", true],
  ])("status %s código %s → corregible: %s", (status, code, esperado) => {
    expect(esCorregibleEnElMostrador({ status, code, message: null, fields: {} })).toBe(
      esperado,
    );
  });
});

describe("payload", () => {
  it("la fecha va en hora local, sin zona", () => {
    expect(localNaiveIso(new Date(2026, 8, 25, 23, 50, 7))).toBe("2026-09-25T23:50:07");
  });

  it("fiado lleva el cliente; efectivo lleva lo recibido; el pago es el total", () => {
    const fiado = con(
      { type: "ADD_PRODUCT", product: yerba },
      { type: "SET_PAYMENT_METHOD", id: "p0", method: "account" },
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

describe("pago mixto (B9)", () => {
  /** $1.000 dividido: el primer pago queda en $1.000 explícito y el nuevo vacío. */
  function dividido(...extra: Parameters<typeof saleReducer>[1][]): SaleState {
    return con({ type: "ADD_PRODUCT", product: yerba }, { type: "SPLIT_PAYMENT" }, ...extra);
  }
  function aplicar(base: SaleState, ...acciones: Parameters<typeof saleReducer>[1][]): SaleState {
    return acciones.reduce(saleReducer, base);
  }
  function ids(s: SaleState): string[] {
    return s.payments.map((p) => p.id);
  }

  it("un solo pago manda el mismo JSON de siempre", () => {
    const s = con({ type: "ADD_PRODUCT", product: yerba });
    expect(buildPayload(s, "id", new Date()).tenders).toEqual([
      { payment_method: "cash", amount_ars: "1000.00" },
    ]);
  });

  it("dividir agrega un medio distinto y deja el reparto a cargo del cajero", () => {
    const s = dividido();
    expect(s.payments.map((p) => p.method)).toEqual(["cash", "debit_card"]);
    expect(s.payments.map((p) => p.amount)).toEqual(["1000.00", ""]);
    expect(cannotCharge(s)).toMatch(/sin importe/);
  });

  it("efectivo + débito que suman el total: se cobra, y el vuelto es contra el efectivo", () => {
    const base = dividido();
    const [efectivo, debito] = ids(base);
    const s = aplicar(
      base,
      { type: "SET_PAYMENT_AMOUNT", id: efectivo!, value: "600" },
      { type: "SET_PAYMENT_AMOUNT", id: debito!, value: "400" },
      { type: "SET_CASH_RECEIVED", value: "1000" },
    );
    expect(cashPartCents(s, 100000)).toBe(60000);
    expect(cannotCharge(s)).toBeNull();
    const p = buildPayload(s, "id", new Date());
    expect(p.tenders).toEqual([
      { payment_method: "cash", amount_ars: "600.00" },
      { payment_method: "debit_card", amount_ars: "400.00" },
    ]);
    expect(p.cash_received_ars).toBe("1000.00");
  });

  it("lo recibido tiene que cubrir la parte en efectivo, no el total", () => {
    const base = dividido();
    const [efectivo, debito] = ids(base);
    const s = aplicar(
      base,
      { type: "SET_PAYMENT_AMOUNT", id: efectivo!, value: "600" },
      { type: "SET_PAYMENT_AMOUNT", id: debito!, value: "400" },
    );
    expect(cannotCharge(saleReducer(s, { type: "SET_CASH_RECEIVED", value: "700" }))).toBeNull();
    expect(cannotCharge(saleReducer(s, { type: "SET_CASH_RECEIVED", value: "500" }))).toMatch(
      /parte en efectivo/,
    );
  });

  it("pagos que no suman bloquean el cobro con lo que falta o sobra", () => {
    const base = dividido();
    const [efectivo, debito] = ids(base);
    const falta = aplicar(
      base,
      { type: "SET_PAYMENT_AMOUNT", id: efectivo!, value: "500" },
      { type: "SET_PAYMENT_AMOUNT", id: debito!, value: "400" },
    );
    expect(unassignedCents(falta, 100000)).toBe(10000);
    expect(cannotCharge(falta)).toMatch(/Falta asignar/);
    const sobra = saleReducer(falta, { type: "SET_PAYMENT_AMOUNT", id: efectivo!, value: "700" });
    expect(cannotCharge(sobra)).toMatch(/de más/);
  });

  it("un cambio de precio NO redistribuye: el reparto queda y avisa lo que falta", () => {
    const base = dividido();
    const [efectivo, debito] = ids(base);
    const s = aplicar(
      base,
      { type: "SET_PAYMENT_AMOUNT", id: efectivo!, value: "600" },
      { type: "SET_PAYMENT_AMOUNT", id: debito!, value: "400" },
      { type: "APPLY_PRICES", prices: { p1: "1200.00" }, notice: "Cambió el precio" },
    );
    expect(s.payments.map((p) => p.amount)).toEqual(["600", "400"]);
    expect(cannotCharge(s)).toMatch(/Falta asignar/);
  });

  it("con una parte fiada hace falta el cliente, y el payload lo lleva", () => {
    const base = dividido();
    const [efectivo, segundo] = ids(base);
    const s = aplicar(
      base,
      { type: "SET_PAYMENT_METHOD", id: segundo!, method: "account" },
      { type: "SET_PAYMENT_AMOUNT", id: efectivo!, value: "600" },
      { type: "SET_PAYMENT_AMOUNT", id: segundo!, value: "400" },
    );
    expect(cannotCharge(s)).toMatch(/cliente/);
    const conCliente = saleReducer(s, { type: "SET_CUSTOMER", id: "c1", name: "Ana" });
    expect(cannotCharge(conCliente)).toBeNull();
    expect(buildPayload(conCliente, "id", new Date()).customer_id).toBe("c1");
  });

  it("sin pago en efectivo no se informa efectivo recibido", () => {
    const base = dividido({ type: "SET_CASH_RECEIVED", value: "1000" });
    const [efectivo] = ids(base);
    const s = saleReducer(base, { type: "SET_PAYMENT_METHOD", id: efectivo!, method: "qr" });
    expect(s.cashReceived).toBe("");
    expect(buildPayload(s, "id", new Date()).cash_received_ars).toBeNull();
  });

  it("quitar hasta quedar uno vuelve a seguir el total", () => {
    const base = dividido();
    const [, debito] = ids(base);
    const s = saleReducer(base, { type: "REMOVE_PAYMENT", id: debito! });
    expect(s.payments).toHaveLength(1);
    expect(s.payments[0]?.amount).toBe("");
    expect(buildPayload(s, "id", new Date()).tenders).toEqual([
      { payment_method: "cash", amount_ars: "1000.00" },
    ]);
  });

  it("no ofrece un medio repetido", () => {
    let s = con({ type: "ADD_PRODUCT", product: yerba });
    for (let i = 0; i < 10; i++) s = saleReducer(s, { type: "SPLIT_PAYMENT" });
    const metodos = s.payments.map((p) => p.method);
    expect(new Set(metodos).size).toBe(metodos.length);
    expect(metodos).toHaveLength(7);
  });
});
