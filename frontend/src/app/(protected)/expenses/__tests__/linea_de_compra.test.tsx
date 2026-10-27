import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import { AxiosError, type AxiosResponse } from "axios";
import ExpensesPage from "../page";

// T3 · B-3: una línea de compra no se anula ni se le cambia monto, categoría o
// fecha (ni el proveedor) desde Gastos; el error del servidor se muestra tal cual.

type Mutacion = { mutationFn?: (arg: unknown) => unknown; onError?: (e: unknown) => void };
const mutaciones: Mutacion[] = [];
const toast = jest.fn();
const updateExpense = jest.fn();
const deleteExpense = jest.fn();

jest.mock("@tanstack/react-query", () => ({
  useQuery: jest.fn(),
  useQueryClient: () => ({ invalidateQueries: jest.fn(), setQueriesData: jest.fn() }),
  useMutation: (opts: Mutacion) => {
    mutaciones.push(opts);
    return { isPending: false, mutate: jest.fn() };
  },
}));
jest.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({ user: { tenant_id: "tenant-a", id: "user-a" } }),
}));
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (state: unknown) => unknown) => selector({ add: toast }),
}));
jest.mock("@/services/expenses.service", () => ({
  expensesService: {
    updateExpense: (...a: unknown[]) => updateExpense(...a),
    deleteExpense: (...a: unknown[]) => deleteExpense(...a),
  },
}));
jest.mock("@/components/layout/PageWrapper", () => ({
  PageWrapper: ({ children }: { children: React.ReactNode }) => <main>{children}</main>,
}));
jest.mock("@/features/ingestion/ManualEntryLauncher", () => ({ ManualEntryLauncher: () => null }));
jest.mock("@/components/ui/PeriodFilter", () => ({ PeriodFilter: () => null }));
jest.mock("@/features/customFields/AddColumnButton", () => ({ AddColumnButton: () => null }));

const base = {
  tenant_id: "tenant-a", is_recurring: false, payment_method: "transfer",
  supplier_name: null, supplier_id: null, notes: null, custom_fields: {},
  created_at: "2026-10-01T10:00:00",
};
const entries = [
  {
    ...base, id: "compra-1", amount: 5000, category: "INVENTORY", expense_type: "COGS",
    product_id: "prod-1", transaction_date: "2026-10-02T10:00:00",
    description: "Compra: Yerba", is_purchase_line: true,
  },
  {
    ...base, id: "alquiler-1", amount: 15000, category: "RENT", expense_type: "OPEX",
    product_id: null, transaction_date: "2026-10-01T10:00:00",
    description: "Alquiler del local", is_purchase_line: false,
  },
];

beforeEach(() => {
  window.localStorage.clear();
  mutaciones.length = 0;
  jest.clearAllMocks();
  (useQuery as jest.Mock).mockImplementation(({ queryKey }: { queryKey: string[] }) => {
    const key = queryKey[0];
    let data: unknown = [];
    if (key === "expenses-entries") data = entries;
    if (key === "expenses-date-range") data = null;
    return { data, isLoading: false, isError: false };
  });
});

/** La mutación cuyo `mutationFn` llama a ese método del servicio (no depende del orden). */
function mutacionQueLlama(metodo: jest.Mock): Mutacion {
  for (const m of mutaciones) {
    metodo.mockClear();
    m.mutationFn?.(entries[0]);
    if (metodo.mock.calls.length > 0) return m;
  }
  throw new Error("ninguna mutación llama a ese método");
}

function filaDe(texto: string): HTMLElement {
  const fila = within(screen.getByRole("table")).getByText(texto).closest("tr");
  if (!fila) throw new Error(`sin fila para ${texto}`);
  return fila;
}

test("una línea de compra no ofrece anular; un gasto común sí", () => {
  render(<ExpensesPage />);
  const anularCompra = within(filaDe("Compra: Yerba")).getByRole("button", {
    name: /parte de una compra de mercadería/,
  });
  expect(anularCompra).toBeDisabled();
  expect(within(filaDe("Alquiler del local")).getByRole("button", { name: "Anular gasto" })).toBeEnabled();
});

test("el editor de una línea de compra deja fijos monto, categoría, fecha y proveedor, y explica por qué", () => {
  render(<ExpensesPage />);
  fireEvent.click(within(filaDe("Compra: Yerba")).getByRole("button", { name: "Editar gasto" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByLabelText("Monto")).toBeDisabled();
  expect(within(dialog).getByLabelText("Categoría")).toBeDisabled();
  expect(within(dialog).getByLabelText("Fecha y hora")).toBeDisabled();
  expect(within(dialog).getByLabelText("Descripción")).toBeEnabled();
  expect(within(dialog).getByLabelText("Proveedor")).toBeDisabled();
  expect(within(dialog).getByText(/ajuste desde el producto/)).toBeInTheDocument();
});

test("el editor de un gasto común no cambia", () => {
  render(<ExpensesPage />);
  fireEvent.click(within(filaDe("Alquiler del local")).getByRole("button", { name: "Editar gasto" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByLabelText("Monto")).toBeEnabled();
  expect(within(dialog).getByLabelText("Categoría")).toBeEnabled();
  expect(within(dialog).getByLabelText("Fecha y hora")).toBeEnabled();
  expect(within(dialog).getByLabelText("Proveedor")).toBeEnabled();
});

test("editar y anular muestran el mensaje del servidor (409 PURCHASE_LINE)", () => {
  render(<ExpensesPage />);
  const mensaje = "Este gasto es parte de una compra de mercadería: …";
  const error = new AxiosError("409", "ERR_BAD_REQUEST", undefined, undefined, {
    status: 409,
    data: { detail: { code: "PURCHASE_LINE", message: mensaje } },
  } as AxiosResponse);
  mutacionQueLlama(updateExpense).onError?.(error);
  expect(toast).toHaveBeenLastCalledWith(mensaje, "error");
  toast.mockClear();
  mutacionQueLlama(deleteExpense).onError?.(error);
  expect(toast).toHaveBeenLastCalledWith(mensaje, "error");
});
