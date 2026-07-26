import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { ColumnRiskDecisionsPanel } from "../ColumnRiskDecisionsPanel";
import type {
  ColumnRiskDecision,
  ContextualColumnRisk,
} from "@/services/ingestion.service";

function makeRisk(
  overrides: Partial<ContextualColumnRisk> = {},
): ContextualColumnRisk {
  return {
    context_id: "ctx-1",
    entity_type: "sale",
    source_column: "obs",
    target_field: "notes",
    null_ratio: 0.9,
    affected_rows: 45,
    null_rows: 45,
    invalid_rows: 0,
    field_requirement: "optional",
    mapping_source: "heuristic",
    user_selected: false,
    allowed_actions: ["route_affected_rows_to_others", "drop_column"],
    recommendation: "Revisá o eliminá la columna",
    ...overrides,
  };
}

// Deferred controlable para simular respuestas async ordenadas manualmente.
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// Flush del microtask queue (las promesas siguen resolviendo con fake timers).
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("ColumnRiskDecisionsPanel — F8c decisiones de columnas riesgosas", () => {
  test("empty state: sin riesgos no renderiza nada", () => {
    const { container } = render(
      <ColumnRiskDecisionsPanel
        initialRisks={[]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={jest.fn()}
        onCancelAndComplete={jest.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  test("porcentaje correcto: null_ratio 0.9 → 90% (fix del bug), 0.35 → 35%", () => {
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[
          makeRisk({ source_column: "obs", null_ratio: 0.9 }),
          makeRisk({ source_column: "nota", null_ratio: 0.35 }),
        ]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={jest.fn()}
        onCancelAndComplete={jest.fn()}
      />,
    );
    expect(screen.getByText(/90% vacío/)).toBeInTheDocument();
    expect(screen.getByText(/35% vacío/)).toBeInTheDocument();
    // El bug legacy mostraría "1%" para 0.9 (Math.round(0.9)).
    expect(screen.queryByText(/^1% vacío/)).not.toBeInTheDocument();
    // affected_rows visible.
    expect(screen.getAllByText(/45 fila\(s\) afectada\(s\)/).length).toBe(2);
  });

  test("botones según allowed_actions (ambas acciones)", () => {
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[makeRisk()]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={jest.fn()}
        onCancelAndComplete={jest.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: /Enviar filas afectadas a Otros/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Eliminar columna/i }),
    ).toBeInTheDocument();
  });

  test("allowed_actions vacío → fila informativa, sin botones de acción", () => {
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[makeRisk({ allowed_actions: [] })]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={jest.fn()}
        onCancelAndComplete={jest.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /Enviar filas afectadas a Otros/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Eliminar columna/i }),
    ).not.toBeInTheDocument();
    // Sigue mostrando el diagnóstico.
    expect(screen.getByText(/90% vacío/)).toBeInTheDocument();
  });

  test("exclusión mutua: route y luego drop deja UNA sola decisión (drop)", () => {
    const onDecisionsChange = jest.fn();
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[makeRisk()]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={onDecisionsChange}
        onCancelAndComplete={jest.fn()}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /Enviar filas afectadas a Otros/i }),
    );
    expect(onDecisionsChange).toHaveBeenLastCalledWith([
      {
        context_id: "ctx-1",
        source_column: "obs",
        target_field: "notes",
        action: "route_affected_rows_to_others",
      },
    ]);

    // El botón de route sigue disponible (no está en drop todavía).
    fireEvent.click(screen.getByRole("button", { name: /Eliminar columna/i }));
    const last = onDecisionsChange.mock.calls.at(-1)?.[0] as ColumnRiskDecision[];
    expect(last).toEqual([
      {
        context_id: "ctx-1",
        source_column: "obs",
        target_field: "notes",
        action: "drop_column",
      },
    ]);
    expect(last).toHaveLength(1);
  });

  test("toggle off: click en la acción activa quita la decisión", () => {
    const onDecisionsChange = jest.fn();
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[makeRisk()]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={onDecisionsChange}
        onCancelAndComplete={jest.fn()}
      />,
    );

    const routeBtn = screen.getByRole("button", {
      name: /Enviar filas afectadas a Otros/i,
    });
    fireEvent.click(routeBtn);
    expect(onDecisionsChange).toHaveBeenLastCalledWith([
      expect.objectContaining({ action: "route_affected_rows_to_others" }),
    ]);

    // Click de nuevo en el mismo botón activo → togglea a vacío.
    fireEvent.click(
      screen.getByRole("button", { name: /Enviar filas afectadas a Otros/i }),
    );
    expect(onDecisionsChange).toHaveBeenLastCalledWith([]);
  });

  test("drop visual: aparece 'se eliminará' y el botón de route ya no se ofrece", () => {
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[makeRisk()]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={jest.fn()}
        onCancelAndComplete={jest.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Eliminar columna/i }));
    expect(screen.getByText(/Se eliminará al confirmar/i)).toBeInTheDocument();
    // El botón de route desaparece para esa columna.
    expect(
      screen.queryByRole("button", { name: /Enviar filas afectadas a Otros/i }),
    ).not.toBeInTheDocument();
    // El botón de drop sigue (para poder togglear off).
    expect(
      screen.getByRole("button", { name: /Eliminar columna/i }),
    ).toBeInTheDocument();
  });

  describe("recompute async", () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      jest.runOnlyPendingTimers();
      jest.useRealTimers();
    });

    test("no dispara recompute en el primer render (mismo recomputeKey)", () => {
      const recompute = jest.fn().mockResolvedValue([]);
      render(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k0"
          recompute={recompute}
          onDecisionsChange={jest.fn()}
          onCancelAndComplete={jest.fn()}
        />,
      );
      act(() => {
        jest.advanceTimersByTime(1000);
      });
      expect(recompute).not.toHaveBeenCalled();
    });

    test("poda al recalcular: recompute sin la columna elimina su decisión", async () => {
      const onDecisionsChange = jest.fn();
      const d = deferred<ContextualColumnRisk[]>();
      const recompute = jest.fn().mockReturnValue(d.promise);

      const { rerender } = render(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k0"
          recompute={recompute}
          onDecisionsChange={onDecisionsChange}
          onCancelAndComplete={jest.fn()}
        />,
      );

      // Elegimos drop sobre la columna.
      fireEvent.click(screen.getByRole("button", { name: /Eliminar columna/i }));
      expect(onDecisionsChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ action: "drop_column" }),
      ]);

      // Cambia el recomputeKey → dispara recompute (debounced).
      rerender(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k1"
          recompute={recompute}
          onDecisionsChange={onDecisionsChange}
          onCancelAndComplete={jest.fn()}
        />,
      );
      act(() => {
        jest.advanceTimersByTime(400);
      });
      expect(recompute).toHaveBeenCalledTimes(1);

      // El recompute devuelve un set SIN la columna "obs".
      d.resolve([makeRisk({ source_column: "otra_col", target_field: "amount" })]);
      await flush();

      // La decisión de "obs" se podó.
      expect(onDecisionsChange).toHaveBeenLastCalledWith([]);
      expect(
        screen.queryByRole("button", { name: /Eliminar columna/i }),
      ).toBeInTheDocument(); // la fila nueva sigue teniendo su botón
      expect(screen.queryByText(/Se eliminará al confirmar/i)).not.toBeInTheDocument();
    });

    test("poda al recalcular: recompute con target_field distinto elimina la decisión", async () => {
      const onDecisionsChange = jest.fn();
      const d = deferred<ContextualColumnRisk[]>();
      const recompute = jest.fn().mockReturnValue(d.promise);

      const { rerender } = render(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k0"
          recompute={recompute}
          onDecisionsChange={onDecisionsChange}
          onCancelAndComplete={jest.fn()}
        />,
      );

      // Elegimos drop sobre la columna.
      fireEvent.click(screen.getByRole("button", { name: /Eliminar columna/i }));
      expect(onDecisionsChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ action: "drop_column" }),
      ]);

      // Cambia el recomputeKey → dispara recompute (debounced).
      rerender(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k1"
          recompute={recompute}
          onDecisionsChange={onDecisionsChange}
          onCancelAndComplete={jest.fn()}
        />,
      );
      act(() => {
        jest.advanceTimersByTime(400);
      });
      expect(recompute).toHaveBeenCalledTimes(1);

      // La misma columna sigue riesgosa, pero ahora mapea a otro target_field
      // (el usuario cambió el mapeo mientras tanto): la decisión vieja ya no aplica.
      d.resolve([makeRisk({ target_field: "otro_campo" })]);
      await flush();

      expect(onDecisionsChange).toHaveBeenLastCalledWith([]);
    });

    test("poda al recalcular: recompute sin la acción elegida en allowed_actions elimina la decisión", async () => {
      const onDecisionsChange = jest.fn();
      const d = deferred<ContextualColumnRisk[]>();
      const recompute = jest.fn().mockReturnValue(d.promise);

      const { rerender } = render(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k0"
          recompute={recompute}
          onDecisionsChange={onDecisionsChange}
          onCancelAndComplete={jest.fn()}
        />,
      );

      // Elegimos drop sobre la columna.
      fireEvent.click(screen.getByRole("button", { name: /Eliminar columna/i }));
      expect(onDecisionsChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ action: "drop_column" }),
      ]);

      // Cambia el recomputeKey → dispara recompute (debounced).
      rerender(
        <ColumnRiskDecisionsPanel
          initialRisks={[makeRisk()]}
          recomputeKey="k1"
          recompute={recompute}
          onDecisionsChange={onDecisionsChange}
          onCancelAndComplete={jest.fn()}
        />,
      );
      act(() => {
        jest.advanceTimersByTime(400);
      });
      expect(recompute).toHaveBeenCalledTimes(1);

      // La misma columna sigue riesgosa con el mismo target_field, pero
      // "drop_column" ya no está entre las acciones permitidas.
      d.resolve([
        makeRisk({ allowed_actions: ["route_affected_rows_to_others"] }),
      ]);
      await flush();

      expect(onDecisionsChange).toHaveBeenLastCalledWith([]);
    });

    test("stale ignorado: la respuesta vieja que resuelve tarde no pisa a la nueva", async () => {
      const first = deferred<ContextualColumnRisk[]>();
      const second = deferred<ContextualColumnRisk[]>();
      const recompute = jest
        .fn()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise);

      const baseProps = {
        initialRisks: [makeRisk()],
        recompute,
        onDecisionsChange: jest.fn(),
        onCancelAndComplete: jest.fn(),
      };

      const { rerender } = render(
        <ColumnRiskDecisionsPanel {...baseProps} recomputeKey="k0" />,
      );

      // Primer cambio → primera request en vuelo.
      rerender(<ColumnRiskDecisionsPanel {...baseProps} recomputeKey="k1" />);
      act(() => {
        jest.advanceTimersByTime(400);
      });
      expect(recompute).toHaveBeenCalledTimes(1);

      // Segundo cambio → segunda request en vuelo (aborta la primera).
      rerender(<ColumnRiskDecisionsPanel {...baseProps} recomputeKey="k2" />);
      act(() => {
        jest.advanceTimersByTime(400);
      });
      expect(recompute).toHaveBeenCalledTimes(2);

      // La SEGUNDA resuelve primero, con datos nuevos.
      second.resolve([
        makeRisk({ source_column: "col_nueva", null_ratio: 0.5 }),
      ]);
      await flush();
      expect(screen.getByText(/50% vacío/)).toBeInTheDocument();
      expect(screen.getByText("col_nueva")).toBeInTheDocument();

      // La PRIMERA resuelve tarde con datos viejos → debe ignorarse.
      first.resolve([
        makeRisk({ source_column: "col_vieja", null_ratio: 0.99 }),
      ]);
      await flush();
      expect(screen.queryByText("col_vieja")).not.toBeInTheDocument();
      expect(screen.getByText("col_nueva")).toBeInTheDocument();
    });

    test("fail-soft: un error de recompute conserva los risks previos", async () => {
      const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
      const d = deferred<ContextualColumnRisk[]>();
      const recompute = jest.fn().mockReturnValue(d.promise);

      const baseProps = {
        initialRisks: [makeRisk({ source_column: "obs", null_ratio: 0.9 })],
        recompute,
        onDecisionsChange: jest.fn(),
        onCancelAndComplete: jest.fn(),
      };

      const { rerender } = render(
        <ColumnRiskDecisionsPanel {...baseProps} recomputeKey="k0" />,
      );
      rerender(<ColumnRiskDecisionsPanel {...baseProps} recomputeKey="k1" />);
      act(() => {
        jest.advanceTimersByTime(400);
      });

      d.reject(new Error("boom"));
      await flush();

      // Los risks previos siguen visibles; el panel no se rompió.
      expect(screen.getByText(/90% vacío/)).toBeInTheDocument();
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });
  });

  test("cancelar global: click llama onCancelAndComplete", () => {
    const onCancelAndComplete = jest.fn();
    render(
      <ColumnRiskDecisionsPanel
        initialRisks={[makeRisk()]}
        recomputeKey="k0"
        recompute={jest.fn()}
        onDecisionsChange={jest.fn()}
        onCancelAndComplete={onCancelAndComplete}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Cancelar y completar datos/i }),
    );
    expect(onCancelAndComplete).toHaveBeenCalledTimes(1);
  });
});
