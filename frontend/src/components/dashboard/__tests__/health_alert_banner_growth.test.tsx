/**
 * M·2d-3: GROWTH_STAGNANT era un riesgo que el banner no conocía (no se
 * mostraba nada). Ahora tiene su entrada.
 */
import "@testing-library/jest-dom";
import { act, render, screen } from "@testing-library/react";

import { HealthAlertBanner } from "../HealthAlertBanner";
import type { HealthScoreV2Response } from "@/types/api";

jest.mock("next/navigation", () => ({ useRouter: () => ({ push: jest.fn() }) }));

test("GROWTH_STAGNANT se muestra en el banner", () => {
  jest.useFakeTimers();
  const score = { primary_risk_code: "GROWTH_STAGNANT", score_total: 60 } as HealthScoreV2Response;
  render(<HealthAlertBanner score={score} />);
  act(() => {
    jest.advanceTimersByTime(900);
  });
  expect(screen.getByText("Ventas estancadas")).toBeInTheDocument();
  jest.useRealTimers();
});
