import { avisoCostoDesactualizado, avisoDeSerieDeMargen, describirMargen, etiquetaDelTramo, nombreDelMes } from "@/features/dashboard/marginText";
import type { MarginFact } from "@/types/api";

// M·3-2: la pantalla sólo formatea lo que manda FactsService.
function fact(over: Partial<MarginFact>): MarginFact {
  return {
    value: 25,
    estado: "confirmado",
    motivo: null,
    periodo: "mes_2026_09",
    desde: "2026-09-01",
    hasta: "2026-09-30",
    cobertura_confirmada_pct: 90,
    cobertura_total_pct: 90,
    ingreso_ars: 1000,
    costo_vendido_ars: 600,
    gastos_operativos_ars: 150,
    resultado_ars: 250,
    categoria: null,
    ...over,
  };
}

describe("describirMargen", () => {
  it("confirmado: el número con su cobertura", () => {
    expect(describirMargen(fact({}))).toEqual({
      valor: "25,0%",
      rotulo: null,
      detalle: "cobertura de costo 90,0%",
      tono: "normal",
    });
  });

  it("estimado: rotulado, con el motivo", () => {
    const d = describirMargen(
      fact({ estado: "estimado", motivo: "cobertura confirmada 20,0%: incluye costos estimados" }),
    );
    expect(d.rotulo).toBe("estimado");
    expect(d.detalle).toBe(
      "cobertura confirmada 20,0%: incluye costos estimados · cobertura de costo 90,0%",
    );
  });

  it("ausente: «—» y el motivo, nunca 0", () => {
    const d = describirMargen(fact({ value: null, estado: "ausente", motivo: "sin gastos cargados en el mes" }));
    expect(d).toEqual({
      valor: "—",
      rotulo: "sin medir",
      detalle: "sin gastos cargados en el mes",
      tono: "ausente",
    });
    expect(describirMargen(null).valor).toBe("—");
  });

  it("negativo vale, en rojo", () => {
    expect(describirMargen(fact({ value: -12.5 }))).toMatchObject({ valor: "-12,5%", tono: "negativo" });
  });
});

describe("nombres de tramo", () => {
  it("mes, semana y día", () => {
    expect(nombreDelMes(fact({}))).toBe("septiembre 2026");
    expect(etiquetaDelTramo(fact({ desde: "2026-09-28" }), "semana")).toBe("sem. 28/09");
    expect(etiquetaDelTramo(fact({ desde: "2026-10-05" }), "dia")).toBe("05/10");
    expect(etiquetaDelTramo(fact({}), "mes")).toBe("septiembre 2026");
    expect(nombreDelMes({ desde: null })).toBe("el período");
  });
});

describe("avisoDeSerieDeMargen", () => {
  const base = {
    sin_datos: false, motivo: null, granularidad: "dia" as const,
    neto_mes_cerrado: null, bruto_mes_en_curso: null, bruto_periodo: null,
    serie_bruto: [] as MarginFact[], serie_neto: [] as MarginFact[], por_categoria: [] as MarginFact[],
  };

  it("neto con un período dentro del mes en curso: explica que es por mes cerrado", () => {
    expect(avisoDeSerieDeMargen("margen_neto", base, false)).toMatch(/mes cerrado/);
  });

  it("sin datos, error y serie sin números", () => {
    expect(avisoDeSerieDeMargen("margen", { ...base, sin_datos: true, motivo: "faltan datos" }, false)).toBe("faltan datos");
    expect(avisoDeSerieDeMargen("margen", undefined, true)).toMatch(/No se pudo/);
    expect(avisoDeSerieDeMargen("margen", { ...base, serie_bruto: [fact({ value: null })] }, false)).toMatch(/No hay margen bruto/);
  });

  it("con puntos: sin aviso", () => {
    expect(avisoDeSerieDeMargen("margen", { ...base, serie_bruto: [fact({})] }, false)).toBeNull();
    expect(avisoDeSerieDeMargen("margen_neto", { ...base, serie_neto: [fact({})] }, false)).toBeNull();
  });
});

describe("avisoCostoDesactualizado (M·3-4)", () => {
  const base = {
    value: 30, estado: "confirmado" as const, motivo: null, periodo: "p", desde: "2026-10-01",
    hasta: "2026-10-17", cobertura_confirmada_pct: 80, cobertura_total_pct: 90, ingreso_ars: 1000,
    costo_vendido_ars: 700, gastos_operativos_ars: null, resultado_ars: null, categoria: null,
  };

  it("avisa con el % que manda el backend", () => {
    expect(avisoCostoDesactualizado({ ...base, costo_desactualizado_pct: 12.5 })).toBe(
      "Costo desactualizado: 12,5% de lo vendido usó un costo de hace más de 45 días",
    );
  });

  it("dice la antigüedad desconocida: nunca se lee como al día", () => {
    expect(
      avisoCostoDesactualizado({ ...base, costo_desactualizado_pct: 0, costo_antiguedad_desconocida_pct: 80 }),
    ).toBe("Antigüedad del costo desconocida en 80,0% de lo vendido");
    expect(
      avisoCostoDesactualizado({
        ...base, costo_desactualizado_pct: 10, costo_antiguedad_desconocida_pct: 5,
        costo_desactualizado_umbral_dias: 60,
      }),
    ).toBe(
      "Costo desactualizado: 10,0% de lo vendido usó un costo de hace más de 60 días; antigüedad del costo desconocida en 5,0% de lo vendido",
    );
  });

  it("sin desactualizado, sin margen o sin dato no dice nada", () => {
    expect(avisoCostoDesactualizado({ ...base, costo_desactualizado_pct: 0 })).toBeNull();
    expect(avisoCostoDesactualizado({ ...base, costo_desactualizado_pct: null })).toBeNull();
    expect(avisoCostoDesactualizado({ ...base, value: null, costo_desactualizado_pct: 40 })).toBeNull();
    expect(avisoCostoDesactualizado(null)).toBeNull();
  });
});
