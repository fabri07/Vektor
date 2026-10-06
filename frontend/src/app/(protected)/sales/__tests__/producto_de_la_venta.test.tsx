/**
 * La columna Producto de /sales no afirma «Producto no encontrado» de más:
 * - mientras cargan los productos todavía no se sabe;
 * - una venta de un producto archivado sigue mostrando su nombre (la lista
 *   traía sólo los activos).
 */
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import SalesPage from "../page";

jest.mock("@tanstack/react-query", () => ({
  useQuery: jest.fn(),
  useQueryClient: () => ({ invalidateQueries: jest.fn(), setQueriesData: jest.fn() }),
  useMutation: () => ({ isPending: false, mutate: jest.fn() }),
}));
jest.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({ user: { tenant_id: "tenant-a", id: "user-a" } }),
}));
jest.mock("@/components/layout/PageWrapper", () => ({
  PageWrapper: ({ children }: { children: React.ReactNode }) => <main>{children}</main>,
}));
jest.mock("@/features/ingestion/ManualEntryLauncher", () => ({ ManualEntryLauncher: () => null }));
jest.mock("@/features/cash/CashCloseButton", () => ({ CashCloseButton: () => null }));
jest.mock("@/components/ui/PeriodFilter", () => ({ PeriodFilter: () => null }));
jest.mock("@/features/customFields/AddColumnButton", () => ({ AddColumnButton: () => null }));

const venta = {
  id: "v1", amount: 12000, quantity: 1,
  transaction_date: "2026-10-06T15:37:00Z", payment_method: "debit_card",
  notes: "", description: "", category: "OTHER", is_recurring: false,
  product_id: "p-archivado", customer_id: null, custom_fields: {},
};
const archivado = { id: "p-archivado", name: "Absorbente diatomita", is_active: false };

function conProductos(productos: { data: unknown[]; isLoading: boolean }) {
  (useQuery as jest.Mock).mockImplementation(({ queryKey }: { queryKey: string[] }) => {
    const [key, variante] = queryKey;
    if (key === "products-list") {
      // La página tiene que pedir también los archivados.
      expect(variante).toBe("con-archivados");
      return { ...productos, isError: false };
    }
    let data: unknown = [];
    if (key === "sales-entries") data = [venta];
    if (key === "sales-entries-count") data = 1;
    if (key === "sales-date-range") data = null;
    return { data, isLoading: false, isError: false };
  });
}

beforeEach(() => {
  window.localStorage.clear();
  jest.clearAllMocks();
});

it("mientras cargan los productos no dice «Producto no encontrado»", () => {
  conProductos({ data: [], isLoading: true });
  render(<SalesPage />);
  const tabla = screen.getByRole("table");
  expect(within(tabla).queryByText("Producto no encontrado")).not.toBeInTheDocument();
  expect(within(tabla).getByText("…")).toBeInTheDocument();
});

it("una venta de un producto archivado muestra su nombre", () => {
  conProductos({ data: [archivado], isLoading: false });
  render(<SalesPage />);
  expect(within(screen.getByRole("table")).getByText("Absorbente diatomita")).toBeInTheDocument();
});
