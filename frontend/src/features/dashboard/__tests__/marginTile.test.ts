import { marginTile } from "@/features/dashboard/HealthScoreCard";
import type { HealthScoreV2Response } from "@/types/api";

// M·2b-3: el subscore de margen nunca se muestra como 0 cuando falta el costo.
function score(over: Partial<HealthScoreV2Response>): HealthScoreV2Response {
  return {
    id: "s1",
    tenant_id: "t1",
    score_total: 60,
    score_cash: 50,
    score_margin: 70,
    score_stock: 50,
    score_supplier: 50,
    score_growth: 50,
    primary_risk_code: "CASH_LOW",
    confidence_level: "MEDIUM",
    data_completeness_score: 70,
    level: "healthy",
    created_at: "2026-10-12T00:00:00Z",
    ...over,
  };
}

describe("marginTile", () => {
  it("margen ausente: sin valor y con la cobertura, nunca 0", () => {
    const tile = marginTile(
      score({ score_margin: null, margin_status: "ausente", cost_coverage_total_pct: 9 }),
    );
    expect(tile.value).toBeNull();
    expect(tile.hint).toBe("sin costo suficiente (cobertura 9%)");
  });

  it("margen ausente sin cobertura conocida", () => {
    expect(marginTile(score({ score_margin: null })).hint).toBe("sin costo suficiente");
  });

  it("margen estimado: valor con rótulo", () => {
    const tile = marginTile(score({ score_margin: 42, margin_status: "estimado" }));
    expect(tile).toEqual({ label: "Margen", value: 42, hint: "estimado" });
  });

  it("margen confirmado o snapshot viejo: sólo el valor", () => {
    expect(marginTile(score({ margin_status: "confirmado" }))).toEqual({
      label: "Margen",
      value: 70,
    });
    expect(marginTile(score({}))).toEqual({ label: "Margen", value: 70 });
  });
});
