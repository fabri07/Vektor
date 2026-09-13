import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import type { AvailableField } from "@/services/fieldCatalog.service";
import { leerPreferenciasDeColumnas } from "@/lib/columnPreferences";
import SalesPage from "../page";
import ExpensesPage from "../../expenses/page";

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

const entries = [{
  id: "record-1", amount: 1200, quantity: 1,
  transaction_date: "2026-09-01T12:00:00Z", payment_method: "cash",
  notes: "Una operación", description: "Una operación", category: "OTHER",
  expense_type: "OPEX", is_recurring: false, product_id: null, customer_id: null,
  supplier_name: null, custom_fields: { condicion: "Conservar embalaje" },
}];

function historicalField(entity: "sale" | "expense"): AvailableField {
  return {
    field_id: `${entity}:custom_fields.condicion`, entity_type: entity,
    field_key: "condicion", value_path: "custom_fields.condicion", label: "Condición especial",
    data_type: "text", origin: "additional", unit: null, enum_options: null,
    editable: false, exportable: true, searchable: true, default_visible: false,
    enabled_for_new_entries: false, financial_rule: null,
  };
}

beforeEach(() => {
  window.localStorage.clear();
  jest.clearAllMocks();
});

test.each([
  ["sale", "sales", SalesPage],
  ["expense", "expenses", ExpensesPage],
] as const)("%s: dato histórico oculto accesible en detalle y columna, preferencia persiste", (entity, section, Page) => {
  (useQuery as jest.Mock).mockImplementation(({ queryKey }: { queryKey: string[] }) => {
    const key = queryKey[0];
    let data: unknown = [];
    if (key === `${section}-entries`) data = entries;
    if (key === `${section}-entries-count`) data = 1;
    if (key === `${section}-date-range`) data = null;
    if (key === "fields-available") data = [historicalField(entity)];
    return { data, isLoading: false, isError: false };
  });

  const first = render(<Page />);
  expect(screen.queryByText("Conservar embalaje")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Ver todos los datos" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText("Conservar embalaje")).toBeInTheDocument();
  expect(within(dialog).getByText("No disponible para nuevas cargas")).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "Columnas" }));
  fireEvent.click(screen.getByRole("button", { name: "Condición especial" }));
  expect(within(screen.getByRole("table")).getByText("Conservar embalaje")).toBeInTheDocument();
  const prefs = leerPreferenciasDeColumnas(`tenant-a:user-a:${section}`);
  expect(prefs?.visible).toContain(`${entity}:custom_fields.condicion`);
  first.unmount();
  render(<Page />);
  expect(within(screen.getByRole("table")).getByText("Conservar embalaje")).toBeInTheDocument();
});
