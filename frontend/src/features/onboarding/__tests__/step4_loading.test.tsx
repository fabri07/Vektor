import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Step4Loading } from "../Step4Loading";

const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
}));

describe("Step4Loading — cierre del onboarding", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  /**
   * Véktor no depura los datos de un archivo a la primera: el usuario tiene
   * que decir qué significa cada columna. Hasta que confirme ese mapeo no hay
   * nada importado, así que tampoco puede haber puntaje. El paso mostraba uno
   * igual — y como el backend todavía no tenía snapshot, salía `NaN/100`.
   */
  test("con archivo subido manda a revisarlo, sin puntaje", async () => {
    const user = userEvent.setup();
    render(<Step4Loading uploadedFileId="file-abc" />);

    expect(screen.getByText(/falta que revises tu archivo/i)).toBeInTheDocument();
    expect(screen.queryByText("/100")).not.toBeInTheDocument();
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /revisar mi archivo/i }));

    expect(mockReplace).toHaveBeenCalledWith("/ingestion?file=file-abc");
  });

  test("sin archivo pide datos, sin puntaje", async () => {
    const user = userEvent.setup();
    render(<Step4Loading uploadedFileId={null} />);

    expect(screen.getByText(/todavía no hay datos/i)).toBeInTheDocument();
    expect(screen.queryByText("/100")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /cargar mis datos/i }));

    expect(mockReplace).toHaveBeenCalledWith("/ingestion");
  });

  test("nunca promete un análisis que todavía no existe", () => {
    render(<Step4Loading uploadedFileId="file-abc" />);

    expect(screen.queryByText(/analizando tu negocio/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/puntaje de salud financiera/i),
    ).not.toBeInTheDocument();
  });
});
