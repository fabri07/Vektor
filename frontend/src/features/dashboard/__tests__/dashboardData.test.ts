import { buildLineSeries } from "@/features/dashboard/dashboardData";
import type { SaleEntryResponse } from "@/services/sales.service";
import type { ExpenseEntryResponse } from "@/services/expenses.service";
import type { HealthScoreV2Response } from "@/types/api";

function sale(overrides: Partial<SaleEntryResponse>): SaleEntryResponse {
  return {
    id: "s1",
    tenant_id: "t1",
    product_id: null,
    customer_id: null,
    amount: 100,
    quantity: 1,
    transaction_date: "2025-01-01T10:00:00",
    payment_method: "cash",
    notes: null,
    created_at: "2025-01-01T10:00:00",
    ...overrides,
  };
}

function expense(overrides: Partial<ExpenseEntryResponse>): ExpenseEntryResponse {
  return {
    id: "e1",
    tenant_id: "t1",
    amount: 50,
    category: "OTHER",
    transaction_date: "2025-01-01T10:00:00",
    description: "",
    is_recurring: false,
    payment_method: "cash",
    supplier_name: null,
    supplier_id: null,
    notes: null,
    created_at: "2025-01-01T10:00:00",
    ...overrides,
  };
}

function score(overrides: Partial<HealthScoreV2Response>): HealthScoreV2Response {
  return {
    id: "sc1",
    tenant_id: "t1",
    score_total: 70,
    score_cash: 70,
    score_margin: 70,
    score_stock: 70,
    score_supplier: 70,
    score_growth: null,
    primary_risk_code: "NONE",
    confidence_level: "HIGH",
    data_completeness_score: 100,
    level: "GOOD",
    created_at: "2025-01-01T00:00:00",
    ...overrides,
  };
}

describe("buildLineSeries — buckets sobre el rango elegido, no sobre 'hoy'", () => {
  it("daily: un punto por día calendario del rango, sin importar la fecha actual", () => {
    const sales = [
      sale({ amount: 500, transaction_date: "2025-01-05T09:00:00" }),
      sale({ amount: 300, transaction_date: "2025-01-07T15:00:00" }),
    ];
    const series = buildLineSeries(
      "ventas",
      sales,
      [],
      "daily",
      { from: "2025-01-05", to: "2025-01-07" },
    );

    expect(series).toHaveLength(3);
    expect(series[0]).toMatchObject({ value: 500 });
    expect(series[1]).toMatchObject({ value: 0 });
    expect(series[2]).toMatchObject({ value: 300 });
  });

  it("weekly: agrupa de verdad varios días en un solo punto por semana", () => {
    // 2025-01-06 es lunes; 2025-01-08 y 2025-01-10 caen en la misma semana.
    const sales = [
      sale({ amount: 200, transaction_date: "2025-01-08T12:00:00" }),
      sale({ amount: 300, transaction_date: "2025-01-10T12:00:00" }),
      sale({ amount: 400, transaction_date: "2025-01-14T12:00:00" }), // semana siguiente
    ];
    const series = buildLineSeries(
      "ventas",
      sales,
      [],
      "weekly",
      { from: "2025-01-06", to: "2025-01-19" },
    );

    expect(series).toHaveLength(2);
    expect(series[0]!.value).toBe(500);
    expect(series[1]!.value).toBe(400);
  });

  it("monthly: agrupa un mes completo en un solo punto", () => {
    const sales = [
      sale({ amount: 100, transaction_date: "2025-01-03T00:00:00" }),
      sale({ amount: 200, transaction_date: "2025-01-28T00:00:00" }),
      sale({ amount: 900, transaction_date: "2025-02-10T00:00:00" }),
    ];
    const series = buildLineSeries(
      "ventas",
      sales,
      [],
      "monthly",
      { from: "2025-01-01", to: "2025-02-28" },
    );

    expect(series).toHaveLength(2);
    expect(series[0]!.value).toBe(300);
    expect(series[1]!.value).toBe(900);
  });

  it("hourly: agrupa por hora de un día histórico puntual, ignorando otras fechas", () => {
    const sales = [
      sale({ amount: 100, transaction_date: "2025-03-10T09:30:00" }),
      sale({ amount: 50, transaction_date: "2025-03-10T09:45:00" }),
      sale({ amount: 999, transaction_date: "2025-03-11T09:30:00" }), // otro día
    ];
    const series = buildLineSeries(
      "ventas",
      sales,
      [],
      "hourly",
      { from: "2025-03-10", to: "2025-03-10" },
    );

    expect(series).toHaveLength(24);
    const hour9 = series.find((point) => point.label === "09:00");
    expect(hour9!.value).toBe(150);
    expect(series.reduce((sum, point) => sum + (point.value ?? 0), 0)).toBe(150);
  });

  it("rango sin movimientos no rompe y devuelve una serie en cero/gap", () => {
    const series = buildLineSeries(
      "ventas",
      [],
      [],
      "daily",
      { from: "2025-06-01", to: "2025-06-03" },
    );
    expect(series).toHaveLength(3);
    expect(series.every((point) => point.value === 0)).toBe(true);
  });

  it("sólo ventas (sin gastos): margen no rompe y da 100% cuando no hubo costo", () => {
    const sales = [sale({ amount: 1000, transaction_date: "2025-01-10T00:00:00" })];
    const series = buildLineSeries(
      "margen",
      sales,
      [],
      "daily",
      { from: "2025-01-10", to: "2025-01-10" },
    );
    expect(series[0]!.value).toBe(100);
  });

  it("sólo gastos (sin ventas): margen queda null (gap), no fabrica un valor", () => {
    const expenses = [expense({ amount: 500, transaction_date: "2025-01-10T00:00:00" })];
    const series = buildLineSeries(
      "margen",
      [],
      expenses,
      "daily",
      { from: "2025-01-10", to: "2025-01-10" },
    );
    expect(series[0]!.value).toBeNull();
  });

  it("stock sin scoreHistory da null (no-invention) — ya no fabrica un coseno", () => {
    const series = buildLineSeries(
      "stock",
      [],
      [],
      "daily",
      { from: "2025-01-01", to: "2025-01-03" },
    );
    expect(series.every((point) => point.value === null)).toBe(true);
  });

  it("stock con scoreHistory usa el score real más cercano a cada bucket", () => {
    const scoreHistory = [
      score({ created_at: "2025-01-01T00:00:00", score_stock: 40 }),
      score({ created_at: "2025-01-03T00:00:00", score_stock: 80 }),
    ];
    const series = buildLineSeries(
      "stock",
      [],
      [],
      "daily",
      { from: "2025-01-01", to: "2025-01-03" },
      scoreHistory,
    );
    expect(series[0]!.value).toBe(40);
    expect(series[2]!.value).toBe(80);
  });
});
