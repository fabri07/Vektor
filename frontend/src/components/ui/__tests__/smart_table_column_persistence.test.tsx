import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";

import { SmartTable, type SmartColumn } from "@/components/ui/SmartTable";
import { leerPreferenciasDeColumnas } from "@/lib/columnPreferences";

/**
 * Cambio 2 (docs/plans/conservacion-y-acceso-datos-negocio.md): las
 * preferencias de columnas sobreviven a un remount (simula recargar la
 * página) cuando se pasa `storageKey`, y NO se filtran entre claves
 * distintas (tenant/usuario/sección) ni afectan al uso sin `storageKey`
 * (comportamiento de siempre, en memoria).
 */

interface Fila {
  id: string;
  a: string;
  b: string;
}

const DATA: Fila[] = [{ id: "1", a: "A1", b: "B1" }];

function columnas(): SmartColumn<Fila>[] {
  return [
    { key: "a", header: "Columna A" },
    { key: "b", header: "Columna B", defaultVisible: false },
  ];
}

beforeEach(() => {
  window.localStorage.clear();
});

test("ocultar/mostrar una columna sobrevive a un remount con la misma storageKey", () => {
  const { unmount } = render(
    <SmartTable columns={columnas()} data={DATA} storageKey="t1:u1:productos" />,
  );

  // "b" arranca oculta (defaultVisible:false) — la muestro.
  fireEvent.click(screen.getByRole("button", { name: /columnas/i }));
  fireEvent.click(screen.getByRole("button", { name: /columna b/i }));
  expect(screen.getByText("B1")).toBeInTheDocument();

  unmount();

  render(<SmartTable columns={columnas()} data={DATA} storageKey="t1:u1:productos" />);
  // Sin volver a tocar nada: "b" sigue visible porque se persistió.
  expect(screen.getByText("B1")).toBeInTheDocument();
});

test("dos storageKey distintas no se pisan entre sí", () => {
  const { unmount } = render(
    <SmartTable columns={columnas()} data={DATA} storageKey="t1:u1:productos" />,
  );
  fireEvent.click(screen.getByRole("button", { name: /columnas/i }));
  fireEvent.click(screen.getByRole("button", { name: /columna b/i }));
  unmount();

  render(<SmartTable columns={columnas()} data={DATA} storageKey="t2:u2:productos" />);
  // Otro tenant/usuario: "b" vuelve a su default (oculta).
  expect(screen.queryByText("B1")).not.toBeInTheDocument();
});

test("sin storageKey no persiste nada (comportamiento de siempre)", () => {
  const { unmount } = render(<SmartTable columns={columnas()} data={DATA} />);
  fireEvent.click(screen.getByRole("button", { name: /columnas/i }));
  fireEvent.click(screen.getByRole("button", { name: /columna b/i }));
  expect(screen.getByText("B1")).toBeInTheDocument();
  unmount();

  render(<SmartTable columns={columnas()} data={DATA} />);
  expect(screen.queryByText("B1")).not.toBeInTheDocument();
  expect(window.localStorage.length).toBe(0);
});

test("una columna nueva se marca «Nueva» hasta que el usuario la toca", () => {
  const { rerender } = render(
    <SmartTable columns={columnas()} data={DATA} storageKey="t1:u1:productos" />,
  );

  const conColumnaNueva: SmartColumn<Fila>[] = [
    ...columnas(),
    { key: "c", header: "Columna C" },
  ];
  rerender(<SmartTable columns={conColumnaNueva} data={DATA} storageKey="t1:u1:productos" />);

  fireEvent.click(screen.getByRole("button", { name: /columnas/i }));
  expect(screen.getByText("Nueva")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /columna c/i }));
  expect(screen.queryByText("Nueva")).not.toBeInTheDocument();
});

test("«Restablecer columnas» borra la preferencia guardada y vuelve al default", () => {
  render(<SmartTable columns={columnas()} data={DATA} storageKey="t1:u1:productos" />);
  fireEvent.click(screen.getByRole("button", { name: /columnas/i }));
  fireEvent.click(screen.getByRole("button", { name: /columna b/i }));
  expect(screen.getByText("B1")).toBeInTheDocument();
  expect(leerPreferenciasDeColumnas("t1:u1:productos")).not.toBeNull();

  fireEvent.click(screen.getByRole("button", { name: /restablecer columnas/i }));
  expect(screen.queryByText("B1")).not.toBeInTheDocument();
  expect(leerPreferenciasDeColumnas("t1:u1:productos")).toBeNull();
});
