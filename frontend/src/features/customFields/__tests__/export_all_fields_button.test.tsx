import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ExportAllFieldsButton } from "../ExportAllFieldsButton";
import { entityExportService } from "@/services/entityExport.service";
import { downloadBlob } from "@/lib/csv";

jest.mock("@/services/entityExport.service", () => ({
  entityExportService: { exportAll: jest.fn() },
}));
jest.mock("@/lib/csv", () => ({ downloadBlob: jest.fn() }));
jest.mock("@/stores/toastStore", () => ({
  useToastStore: (selector: (s: { add: jest.Mock }) => unknown) => selector({ add: mockToast }),
}));

const mockExportAll = entityExportService.exportAll as jest.Mock;
const mockDownloadBlob = downloadBlob as jest.Mock;
const mockToast = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
});

test("pide la exportación completa y dispara la descarga", async () => {
  const blob = new Blob(["a,b\n1,2"], { type: "text/csv" });
  mockExportAll.mockResolvedValue(blob);

  render(<ExportAllFieldsButton entityType="product" entityLabel="Productos" />);
  fireEvent.click(screen.getByRole("button", { name: /exportar todo/i }));

  await waitFor(() => expect(mockDownloadBlob).toHaveBeenCalledTimes(1));
  expect(mockExportAll).toHaveBeenCalledWith("product", false);
  const [filename, downloadedBlob] = mockDownloadBlob.mock.calls[0];
  expect(filename).toMatch(/^vektor-product-completo-\d{4}-\d{2}-\d{2}\.csv$/);
  expect(downloadedBlob).toBe(blob);
});

test("includeInactive se pasa al servicio", async () => {
  mockExportAll.mockResolvedValue(new Blob());
  render(
    <ExportAllFieldsButton entityType="supplier" entityLabel="Proveedores" includeInactive />,
  );
  fireEvent.click(screen.getByRole("button", { name: /exportar todo/i }));

  await waitFor(() => expect(mockExportAll).toHaveBeenCalledWith("supplier", true));
});

test("un error de red avisa por toast, no rompe la pantalla", async () => {
  mockExportAll.mockRejectedValue(new Error("network"));
  render(<ExportAllFieldsButton entityType="product" entityLabel="Productos" />);
  fireEvent.click(screen.getByRole("button", { name: /exportar todo/i }));

  await waitFor(() =>
    expect(mockToast).toHaveBeenCalledWith(
      "No se pudo exportar Productos. Probá de nuevo.",
      "error",
    ),
  );
  expect(mockDownloadBlob).not.toHaveBeenCalled();
});

test("el botón se deshabilita mientras exporta", async () => {
  let resolver: (b: Blob) => void = () => {};
  mockExportAll.mockReturnValue(new Promise<Blob>((r) => (resolver = r)));

  render(<ExportAllFieldsButton entityType="product" entityLabel="Productos" />);
  const boton = screen.getByRole("button", { name: /exportar todo/i });
  fireEvent.click(boton);

  await waitFor(() => expect(boton).toBeDisabled());
  resolver(new Blob());
  await waitFor(() => expect(boton).not.toBeDisabled());
});
