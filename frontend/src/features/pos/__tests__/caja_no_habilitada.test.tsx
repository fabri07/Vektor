/**
 * Prueba en "Limpieza del Sur" (2026-10): lo que la caja dice tiene que llevar
 * a la solución.
 *
 * - Una venta trabada porque la PC no es caja (`TERMINAL_*`) decía «Hay que
 *   volver a entrar»: el cajero re-logueaba y la venta seguía trabada.
 * - El descuento aceptaba texto y lo dejaba a la vista sin aplicarlo.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import { MoneyInput } from "@/features/pos/DiscountControl";
import { PendingOperationsPanel } from "@/features/pos/PendingOperationsPanel";
import type { OperacionGuardada } from "@/lib/offline/posDb";

jest.mock("@/services/pos.service", () => ({ posService: {} }));

function trabada(codigo: string, error: string): OperacionGuardada {
  return {
    clientOperationId: "op-1",
    tenantId: "t1",
    userId: "u1",
    terminalId: null,
    estado: "REQUIERE_AUTENTICACION",
    payload: {
      client_operation_id: "op-1",
      tenders: [{ payment_method: "debit_card", amount_ars: "12000.00" }],
    },
    lines: [],
    recibo: {},
    creadaEn: "2026-10-06T15:37:00.000Z",
    intentos: 1,
    ultimoError: error,
    ultimoCodigo: codigo,
    proximoIntentoEn: 0,
    resultado: null,
    sincronizadaEn: null,
  } as unknown as OperacionGuardada;
}

function panel(op: OperacionGuardada, esDuenio: boolean) {
  render(
    <PendingOperationsPanel
      pendientes={[op]}
      terminalId={null}
      esDuenio={esDuenio}
      sincronizando={false}
      persistencia={true}
      onFlush={jest.fn()}
      onCambio={jest.fn()}
      onImprimir={jest.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Ventas sin sincronizar/ }));
}

const SIN_CAJA = "Una venta cobrada sin conexión sólo se acepta desde una caja habilitada.";

it("sin caja habilitada no manda a volver a entrar: lleva a habilitarla", () => {
  panel(trabada("TERMINAL_REQUIRED_FOR_CAPTURE", SIN_CAJA), true);
  expect(screen.getByText("Esta PC no está habilitada como caja")).toBeInTheDocument();
  expect(screen.queryByText("Hay que volver a entrar")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Habilitar esta PC como caja" })).toHaveAttribute(
    "href",
    "/settings?tab=seguridad",
  );
});

it("al cajero le dice que la habilite el dueño", () => {
  panel(trabada("TERMINAL_DISABLED", "Esta caja fue dada de baja."), false);
  expect(screen.getByText(/Pedile al dueño que habilite esta PC como caja/)).toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});

it("una sesión vencida sí pide volver a entrar", () => {
  panel(trabada("SESSION_EXPIRED", "La sesión venció."), true);
  expect(screen.getByText("Hay que volver a entrar")).toBeInTheDocument();
  expect(screen.queryByText("Esta PC no está habilitada como caja")).not.toBeInTheDocument();
});

it("un importe no acepta letras", () => {
  const onCents = jest.fn();
  render(<MoneyInput cents={0} onCents={onCents} label="Descuento a la venta" />);
  const campo = screen.getByLabelText("Descuento a la venta") as HTMLInputElement;
  fireEvent.change(campo, { target: { value: "canasto" } });
  expect(campo.value).toBe("");
  fireEvent.change(campo, { target: { value: "150,50" } });
  expect(campo.value).toBe("150,50");
  expect(onCents).toHaveBeenLastCalledWith(15050);
});
