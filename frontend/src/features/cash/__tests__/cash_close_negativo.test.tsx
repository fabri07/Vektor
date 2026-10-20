/**
 * M·2d-3: un esperado de efectivo negativo (se retiró más de lo que entró) es
 * válido: contar el fondo menos esa diferencia es «caja cuadrada», y el modal
 * manda el contado negativo tal cual (antes lo recortaba a 0).
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CashCloseModal } from "../CashCloseModal";

const mockPreview = jest.fn();
const mockCreate = jest.fn();
jest.mock("@/services/cashClose.service", () => ({
  cashCloseService: {
    getPreview: (...a: unknown[]) => mockPreview(...a),
    createClose: (...a: unknown[]) => mockCreate(...a),
  },
}));

function preview(breakdown: { payment_method: string; expected_ars: number }[]) {
  return {
    close_date: "2026-10-19",
    expected_total_ars: breakdown.reduce((s, b) => s + b.expected_ars, 0),
    breakdown,
    already_closed: false,
    is_past_close_now: false,
    suggested_opening_float_ars: null,
    fiscal_condition: null,
  };
}

function pintar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CashCloseModal isOpen onClose={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockCreate.mockResolvedValue({ surplus_registered: false });
});

test("arqueo completo: fondo 100, 70 en el cajón y esperado −30 cierra cuadrado", async () => {
  mockPreview.mockResolvedValue(preview([{ payment_method: "cash", expected_ars: -30 }]));
  pintar();
  expect(await screen.findByText(/salió más efectivo del que entró/)).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText(/Fondo de caja inicial/), { target: { value: "100" } });
  fireEvent.change(screen.getByLabelText("$20"), { target: { value: "3" } });
  fireEvent.change(screen.getByLabelText("$10"), { target: { value: "1" } });
  expect(screen.getByText("Caja cuadrada")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Cerrar caja" }));
  await waitFor(() => expect(mockCreate).toHaveBeenCalled());
  expect(mockCreate.mock.calls[0][0]).toMatchObject({
    counted_total_ars: -30,
    opening_float_ars: 100,
    cash_denominations: { "20": 3, "10": 1 },
  });
});

test("modo simple: manda el neto negativo", async () => {
  mockPreview.mockResolvedValue(preview([{ payment_method: "cash", expected_ars: -30 }]));
  pintar();
  fireEvent.click(await screen.findByRole("button", { name: "Ingresar solo el total" }));
  fireEvent.change(screen.getByLabelText(/sin contar el fondo/), { target: { value: "-30" } });
  fireEvent.click(screen.getByRole("button", { name: "Cerrar caja" }));
  await waitFor(() => expect(mockCreate).toHaveBeenCalled());
  expect(mockCreate.mock.calls[0][0]).toMatchObject({ counted_total_ars: -30 });
});

test("el aviso sale por el efectivo negativo aunque el total sea positivo", async () => {
  mockPreview.mockResolvedValue(
    preview([
      { payment_method: "cash", expected_ars: -30 },
      { payment_method: "transfer", expected_ars: 1000 },
    ]),
  );
  pintar();
  expect(await screen.findByText(/salió más efectivo del que entró/)).toBeInTheDocument();
});

test("sin efectivo negativo no hay aviso, y ningún campo sugiere un negativo", async () => {
  mockPreview.mockResolvedValue(
    preview([
      { payment_method: "cash", expected_ars: 500 },
      { payment_method: "transfer", expected_ars: -50 },
    ]),
  );
  pintar();
  await screen.findByText("Total esperado");
  expect(screen.queryByText(/salió más efectivo del que entró/)).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText("-50")).not.toBeInTheDocument();
});
