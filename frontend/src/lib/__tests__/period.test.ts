import {
  type PeriodValue,
  resolvePeriod,
  resolvePreviousPeriod,
  weeksOfMonth,
  hasMultiYearData,
  selectableYears,
} from "@/lib/period";

// Fecha fija para los presets relativos: miércoles 2026-06-17.
const FIXED_NOW = new Date(2026, 5, 17, 10, 0, 0); // mes 5 = junio

describe("resolvePeriod — kinds absolutos (deterministas)", () => {
  it("year cubre 1-ene a 31-dic", () => {
    expect(resolvePeriod({ kind: "year", year: 2025 })).toEqual({
      from: "2025-01-01",
      to: "2025-12-31",
    });
  });

  it("month cubre el mes completo (incluye fin de mes correcto)", () => {
    expect(resolvePeriod({ kind: "month", year: 2024, month: 2 })).toEqual({
      from: "2024-02-01",
      to: "2024-02-29", // 2024 bisiesto
    });
    expect(resolvePeriod({ kind: "month", year: 2025, month: 2 })).toEqual({
      from: "2025-02-01",
      to: "2025-02-28",
    });
  });

  it("week va de lunes a domingo (7 días)", () => {
    // 2026-06-15 es lunes
    expect(resolvePeriod({ kind: "week", start: "2026-06-15" })).toEqual({
      from: "2026-06-15",
      to: "2026-06-21",
    });
  });

  it("day es un único día", () => {
    expect(resolvePeriod({ kind: "day", date: "2026-03-09" })).toEqual({
      from: "2026-03-09",
      to: "2026-03-09",
    });
  });
});

describe("resolvePeriod — presets (con fecha fija)", () => {
  beforeEach(() => {
    jest.useFakeTimers().setSystemTime(FIXED_NOW.getTime());
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  it("today", () => {
    expect(resolvePeriod({ kind: "preset", preset: "today" })).toEqual({
      from: "2026-06-17",
      to: "2026-06-17",
    });
  });

  it("yesterday", () => {
    expect(resolvePeriod({ kind: "preset", preset: "yesterday" })).toEqual({
      from: "2026-06-16",
      to: "2026-06-16",
    });
  });

  it("this_week arranca el lunes y termina hoy", () => {
    // miércoles 17 → lunes 15
    expect(resolvePeriod({ kind: "preset", preset: "this_week" })).toEqual({
      from: "2026-06-15",
      to: "2026-06-17",
    });
  });

  it("last_week es lunes-domingo de la semana anterior", () => {
    expect(resolvePeriod({ kind: "preset", preset: "last_week" })).toEqual({
      from: "2026-06-08",
      to: "2026-06-14",
    });
  });

  it("this_month arranca el día 1 y termina hoy", () => {
    expect(resolvePeriod({ kind: "preset", preset: "this_month" })).toEqual({
      from: "2026-06-01",
      to: "2026-06-17",
    });
  });

  it("last_month es el mes anterior completo", () => {
    expect(resolvePeriod({ kind: "preset", preset: "last_month" })).toEqual({
      from: "2026-05-01",
      to: "2026-05-31",
    });
  });

  it("domingo se considera fin de la misma semana (no inicio)", () => {
    jest.setSystemTime(new Date(2026, 5, 21, 10, 0, 0).getTime()); // domingo 21
    expect(resolvePeriod({ kind: "preset", preset: "this_week" })).toEqual({
      from: "2026-06-15", // lunes de esa semana
      to: "2026-06-21",
    });
  });
});

describe("resolvePreviousPeriod", () => {
  beforeEach(() => {
    jest.useFakeTimers().setSystemTime(FIXED_NOW.getTime());
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  it("year anterior", () => {
    expect(resolvePreviousPeriod({ kind: "year", year: 2025 })).toEqual({
      from: "2024-01-01",
      to: "2024-12-31",
    });
  });

  it("month anterior cruza el año correctamente", () => {
    expect(resolvePreviousPeriod({ kind: "month", year: 2026, month: 1 })).toEqual({
      from: "2025-12-01",
      to: "2025-12-31",
    });
  });

  it("week anterior es 7 días antes", () => {
    expect(resolvePreviousPeriod({ kind: "week", start: "2026-06-15" })).toEqual({
      from: "2026-06-08",
      to: "2026-06-14",
    });
  });

  it("day anterior", () => {
    expect(resolvePreviousPeriod({ kind: "day", date: "2026-06-17" })).toEqual({
      from: "2026-06-16",
      to: "2026-06-16",
    });
  });

  it("preset this_month compara contra el mes anterior completo", () => {
    expect(
      resolvePreviousPeriod({ kind: "preset", preset: "this_month" }),
    ).toEqual({ from: "2026-05-01", to: "2026-05-31" });
  });
});

describe("helpers del navegador jerárquico", () => {
  it("weeksOfMonth devuelve los lunes que tocan el mes", () => {
    const weeks = weeksOfMonth(2026, 6); // junio 2026
    expect(weeks[0]).toBe("2026-06-01"); // 1-jun es lunes
    expect(weeks.every((w) => /^\d{4}-\d{2}-\d{2}$/.test(w))).toBe(true);
  });

  it("hasMultiYearData es true si min es de un año anterior", () => {
    jest.useFakeTimers().setSystemTime(FIXED_NOW.getTime());
    expect(hasMultiYearData("2024-01-01")).toBe(true);
    expect(hasMultiYearData("2026-01-01")).toBe(false);
    expect(hasMultiYearData(null)).toBe(false);
    jest.useRealTimers();
  });

  it("selectableYears va de hoy hacia atrás hasta el año mínimo", () => {
    jest.useFakeTimers().setSystemTime(FIXED_NOW.getTime());
    expect(selectableYears("2024-05-01")).toEqual([2026, 2025, 2024]);
    expect(selectableYears(null)).toEqual([2026]);
    jest.useRealTimers();
  });
});
