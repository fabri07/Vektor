import {
  CAN_SHARE_FILES_OPTIONS,
  HISTORY_DEPTH_OPTIONS,
  MAIN_CONCERN_OPTIONS,
  RECORDS_FORMAT_OPTIONS,
  REQUESTED_PLAN_OPTIONS,
  REVENUE_BAND_OPTIONS,
  STAFF_SIZE_OPTIONS,
  YEARS_OPERATING_OPTIONS,
  labelOf,
  type Choice,
} from "../accessRequestOptions";

/**
 * Contrato de los rótulos visibles del screening.
 *
 * Por qué existe: los tests de formulario (`access-request/`, `onboarding/`)
 * navegan por estos rótulos para llegar a cada control, y desde 2026-08-26 los
 * IMPORTAN de este catálogo en vez de hardcodearlos — así un copy pass no vuelve
 * a romper 46 tests de comportamiento por un cambio de redacción. Pero eso deja
 * un hueco: si nadie mira el texto, un rótulo se puede cambiar (o romper) sin
 * que falle nada.
 *
 * Este archivo tapa ese hueco y concentra el aviso en UN solo lugar. Un cambio
 * de copy deliberado rompe únicamente este test: se actualiza acá, con el
 * cambio a la vista en el diff, y listo. Un cambio accidental también rompe acá,
 * que es exactamente donde uno quiere enterarse.
 *
 * Los `value` son otro asunto: son el contrato con el backend (espejo de los
 * `StrEnum` de `app/domain/access_request.py`) y cambiarlos es un 422, no un
 * cambio de copy. Se fijan igual, en el mismo lugar.
 */

const CATALOGOS: ReadonlyArray<
  readonly [string, readonly Choice<string>[], Readonly<Record<string, string>>]
> = [
  [
    "YEARS_OPERATING_OPTIONS",
    YEARS_OPERATING_OPTIONS,
    {
      lt_6m: "Menos de 6 meses",
      "6m_2y": "Entre 6 meses y 2 años",
      "2y_5y": "Entre 2 y 5 años",
      gt_5y: "Más de 5 años",
    },
  ],
  [
    "STAFF_SIZE_OPTIONS",
    STAFF_SIZE_OPTIONS,
    {
      solo: "Trabajo por mi cuenta",
      "2_5": "2 a 5",
      "6_15": "6 a 15",
      gt_15: "Más de 15",
    },
  ],
  [
    "MAIN_CONCERN_OPTIONS",
    MAIN_CONCERN_OPTIONS,
    { MARGIN: "Margen", STOCK: "Stock", CASH: "Caja" },
  ],
  [
    "REVENUE_BAND_OPTIONS",
    REVENUE_BAND_OPTIONS,
    {
      lt_3m: "Menos de $3M",
      "3m_10m": "Entre $3M y $10M",
      "10m_30m": "Entre $10M y $30M",
      gt_30m: "Más de $30M",
      no_contesta: "Prefiero no decirlo",
    },
  ],
  [
    "RECORDS_FORMAT_OPTIONS",
    RECORDS_FORMAT_OPTIONS,
    {
      papel: "Cuaderno o papel",
      planilla: "Excel o Google Sheets",
      sistema: "Un sistema de gestión o facturación",
      mixto: "Una mezcla de varias cosas",
      ninguno: "No guardo registros",
    },
  ],
  [
    "HISTORY_DEPTH_OPTIONS",
    HISTORY_DEPTH_OPTIONS,
    {
      lt_6m: "Menos de 6 meses",
      "6m_1y": "Entre 6 meses y 1 año",
      "1y_3y": "Entre 1 y 3 años",
      gt_3y: "Más de 3 años",
      ninguno: "No tengo historial",
    },
  ],
  [
    "CAN_SHARE_FILES_OPTIONS",
    CAN_SHARE_FILES_OPTIONS,
    {
      si_ordenados: "Sí, están ordenados",
      si_desprolijos: "Sí, aunque necesitan orden",
      no: "No los tengo en formato digital",
    },
  ],
  [
    "REQUESTED_PLAN_OPTIONS",
    REQUESTED_PLAN_OPTIONS,
    { free: "Plan Gratuito", premium: "Premium" },
  ],
];

describe("catálogos del screening — contrato de rótulos visibles", () => {
  test.each(CATALOGOS)("%s rinde los rótulos esperados", (_nombre, opciones, esperado) => {
    const real = Object.fromEntries(opciones.map((o) => [o.value, o.label]));
    expect(real).toEqual(esperado);
  });

  // Sin esto, agregar una opción nueva no rompería nada: `toEqual` sobre el
  // objeto sí lo detecta, pero sólo si el catálogo declarado arriba se mantiene
  // completo. Este chequeo lo hace explícito.
  test.each(CATALOGOS)("%s no cambió de tamaño", (_nombre, opciones, esperado) => {
    expect(opciones).toHaveLength(Object.keys(esperado).length);
  });

  test("ningún rótulo queda vacío ni con espacios de sobra", () => {
    for (const [, opciones] of CATALOGOS) {
      for (const { value, label } of opciones) {
        expect(label.trim()).not.toBe("");
        expect(label).toBe(label.trim());
        expect(value.trim()).not.toBe("");
      }
    }
  });
});

describe("labelOf", () => {
  test("devuelve el rótulo de la opción pedida", () => {
    expect(labelOf(MAIN_CONCERN_OPTIONS, "CASH")).toBe("Caja");
    expect(labelOf(REQUESTED_PLAN_OPTIONS, "free")).toBe("Plan Gratuito");
  });

  test("tira si el value no existe, en vez de devolver undefined", () => {
    // Un test que busca un rótulo inexistente tiene que romperse acá, no
    // arrastrar un `undefined` hasta un selector que después falla por otra
    // razón y manda a investigar el lugar equivocado.
    expect(() =>
      labelOf(MAIN_CONCERN_OPTIONS, "NO_EXISTE" as never),
    ).toThrow(/no existe la opción/);
  });
});
