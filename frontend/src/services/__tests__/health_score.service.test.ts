/**
 * `GET /health-scores/latest` tiene TRES formas de respuesta (backend
 * `app/api/v1/health_scores.py`): el score, `{status: "CALCULATING"}` y
 * `{status: "NO_DATA", score: null, …}`.
 *
 * El frontend conocía sólo las dos primeras, así que el payload de `NO_DATA`
 * se trataba como score y `score_total` llegaba `undefined` a la UI —
 * `Math.round(Number(undefined))` = `NaN`, que es lo que veía el usuario
 * recién dado de alta.
 */
import { clasificarLatestScore } from "../health_score.service";

const SCORE_REAL = {
  id: "s-1",
  tenant_id: "t-1",
  score_total: 62,
  score_cash: 50,
  score_margin: 70,
  score_stock: 60,
  score_supplier: 80,
  score_growth: null,
  primary_risk_code: "CASH_LOW",
  confidence_level: "MEDIUM",
  data_completeness_score: 65,
  level: "FAIR",
  created_at: "2026-08-01T00:00:00Z",
};

const NO_DATA = {
  status: "NO_DATA",
  score: null,
  score_level: "NO_DATA",
  is_demo_data: false,
  mensaje: "Cargá tus datos para ver tu análisis",
};

describe("clasificarLatestScore", () => {
  test("un score real es un score", () => {
    expect(clasificarLatestScore(SCORE_REAL)).toBe("score");
  });

  test("CALCULATING no es un score", () => {
    expect(clasificarLatestScore({ status: "CALCULATING" })).toBe("calculating");
  });

  test("NO_DATA no es un score ni un cálculo en curso", () => {
    // Distinguirlos importa: con CALCULATING hay que esperar, con NO_DATA hay
    // que pedirle datos al usuario. Colapsarlos deja a alguien esperando algo
    // que no va a llegar solo.
    expect(clasificarLatestScore(NO_DATA)).toBe("no_data");
  });

  test("un status desconocido nunca se trata como score", () => {
    // Si el backend agrega un cuarto estado, el default seguro es "todavía no
    // hay score" — jamás castearlo y renderizar campos inexistentes.
    expect(clasificarLatestScore({ status: "ALGO_NUEVO" })).not.toBe("score");
  });

  test("ausencia de respuesta tampoco es un score", () => {
    expect(clasificarLatestScore(null)).not.toBe("score");
    expect(clasificarLatestScore(undefined)).not.toBe("score");
  });
});
