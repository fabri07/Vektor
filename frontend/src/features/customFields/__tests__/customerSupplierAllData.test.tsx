import { render, screen, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import CustomersPage from "@/app/(protected)/customers/page";
import SuppliersPage from "@/app/(protected)/suppliers/page";
import { customersService } from "@/services/customers.service";
import { suppliersService } from "@/services/suppliers.service";
import { fieldCatalogService, type AvailableField } from "@/services/fieldCatalog.service";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { useAuthStore } from "@/stores/authStore";

jest.mock("@/services/customers.service");
jest.mock("@/services/suppliers.service");
jest.mock("@/services/fieldCatalog.service");
jest.mock("@/services/fieldDefinitions.service");

const catalogField = (entityType: "customer" | "supplier"): AvailableField => ({
  field_id: `${entityType}:custom:condicion_especial`,
  entity_type: entityType,
  field_key: "condicion_especial",
  label: "Condición especial",
  data_type: "text",
  unit: null,
  origin: "additional",
  value_path: "custom_fields.condicion_especial",
  enum_options: null,
  editable: false,
  exportable: true,
  searchable: true,
  default_visible: false,
  enabled_for_new_entries: false,
  financial_rule: null,
});

describe.each([
  { entityType: "customer" as const, Page: CustomersPage, label: "cliente", section: "customers" },
  { entityType: "supplier" as const, Page: SuppliersPage, label: "proveedor", section: "suppliers" },
])("Todos los datos: $section", ({ entityType, Page, label, section }) => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    useAuthStore.setState({
      user: { id: "user-1", tenant_id: "tenant-1", email: "test@example.com", full_name: "Test", role: "OWNER" },
    });
    jest.mocked(fieldCatalogService.getAvailableFields).mockResolvedValue([catalogField(entityType)]);
    jest.mocked(fieldDefinitionsService.getAll).mockResolvedValue([]);
    const row = {
      id: "record-1",
      name: "Sin identificar",
      is_active: true,
      is_sentinel: true,
      custom_fields: { condicion_especial: "Entrega únicamente los martes" },
    };
    jest.mocked(customersService.getAllCustomers).mockResolvedValue([
      row as unknown as Awaited<ReturnType<typeof customersService.getAllCustomers>>[number],
    ]);
    jest.mocked(suppliersService.getAllSuppliers).mockResolvedValue([
      row as unknown as Awaited<ReturnType<typeof suppliersService.getAllSuppliers>>[number],
    ]);
  });

  it("permite consultar campos históricos ocultos incluso en el centinela, sin habilitar edición", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><Page /></QueryClientProvider>);

    const viewButton = await screen.findByRole("button", { name: `Todos los datos del ${label}` });
    expect(screen.queryByRole("button", { name: `Editar ${label}` })).not.toBeInTheDocument();
    expect(screen.queryByText("Entrega únicamente los martes")).not.toBeInTheDocument();
    fireEvent.click(viewButton);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Entrega únicamente los martes")).toBeInTheDocument();
    expect(within(dialog).getByText("No disponible para nuevas cargas")).toBeInTheDocument();
    expect(fieldCatalogService.getAvailableFields).toHaveBeenCalledWith(entityType);
    expect(screen.getByRole("link", { name: "Sin identificar" })).toHaveAttribute("href", `/${section}/record-1`);
  });
});
