import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

import { RereadProgress } from "../RereadProgress";

describe("RereadProgress — contexto (Fase 10, revisión 2026-08-20)", () => {
  it("sin totalRows/startedAt no muestra ninguna línea de contexto (comportamiento previo)", () => {
    render(<RereadProgress label="Aplicando cambios…" />);
    expect(screen.getByText("Aplicando cambios…")).toBeInTheDocument();
    expect(screen.queryByText(/fila\(s\)/)).not.toBeInTheDocument();
    expect(screen.queryByText(/empezado hace/)).not.toBeInTheDocument();
  });

  it("con totalRows muestra el total conocido desde el preview", () => {
    render(<RereadProgress label="Aplicando cambios…" totalRows={1200} />);
    expect(screen.getByText(/~1[.,]200 fila\(s\)/)).toBeInTheDocument();
  });

  it("con startedAt muestra el cronómetro de tiempo transcurrido", () => {
    const startedAt = new Date(Date.now() - 5_000).toISOString();
    render(<RereadProgress label="Aplicando cambios…" startedAt={startedAt} />);
    expect(screen.getByText(/empezado hace \d+s/)).toBeInTheDocument();
  });

  it("combina ambos con separador cuando los dos están presentes", () => {
    const startedAt = new Date(Date.now() - 2_000).toISOString();
    render(
      <RereadProgress label="Aplicando cambios…" totalRows={50} startedAt={startedAt} />,
    );
    expect(screen.getByText(/~50 fila\(s\) · empezado hace \d+s/)).toBeInTheDocument();
  });
});

describe("RereadProgress — honestidad del avance (2026-08-26)", () => {
  it("no muestra ningún porcentaje: no hay medición real de filas procesadas", () => {
    render(<RereadProgress label="Aplicando cambios…" totalRows={1200} />);
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("usa una barra indeterminada anunciada con la etapa en curso", () => {
    render(<RereadProgress label="Aplicando cambios…" />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-busy", "true");
    expect(bar).toHaveAccessibleName("Aplicando cambios…");
  });

  it("sin label anuncia la etapa que está ciclando, no un rótulo de import", () => {
    render(<RereadProgress />);
    expect(screen.getByRole("progressbar")).toHaveAccessibleName(/…/);
    expect(screen.getByRole("progressbar")).not.toHaveAccessibleName("Importando");
  });
});
