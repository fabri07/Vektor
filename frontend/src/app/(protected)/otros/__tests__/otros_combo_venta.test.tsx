/**
 * Combo («Jean + buzo» $41.000): el servidor propone las combinaciones que suman el
 * monto y la persona confirma; la venta se manda con sus líneas y sólo si la suma
 * cierra al centavo. Nada se reparte por ella.
 */
import React from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type {
  ComboSuggestionsResponse,
  UnclassifiedRecordResponse,
} from "@/services/others.service";
import { ReclassifyModal } from "../ReclassifyModal";
import { errorDeCombo, sumaEnCentavos } from "../ComboVentaLineas";

const comboSuggestions = jest.fn();

jest.mock("@/services/others.service", () => ({
  othersService: { comboSuggestions: (...a: unknown[]) => comboSuggestions(...a) },
}));
jest.mock("@/services/products.service", () => ({
  productsService: {
    getAllProducts: jest.fn().mockResolvedValue([
      { id: "p-jean", name: "Jeans azules T42" },
      { id: "p-buzo", name: "buzo canguro gris L" },
    ]),
  },
}));

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const record = {
  id: "r-combo",
  uploaded_file_id: null,
  source: "ingestion",
  context_label: "Venta sin producto: elegí qué producto se vendió",
  headers: null,
  row_data: { fecha: "2026-10-22", monto: "41000", detalle: "Jean + buzo", medio_de_pago: "cash" },
  suggested_entity: "sale",
  suggested_category: null,
  suggested_category_label: null,
  match_candidates: null,
  status: "PENDING",
  created_at: "2026-10-23T10:00:00Z",
} as unknown as UnclassifiedRecordResponse;

const UNA_SOLUCION: ComboSuggestionsResponse = {
  references: ["Jean", "buzo"],
  amount: "41000",
  solutions: [
    [
      { product_id: "p-jean", product_name: "Jeans azules T42", quantity: 1, unit_price: "21000.00", amount: "21000.00" },
      { product_id: "p-buzo", product_name: "buzo canguro gris L", quantity: 1, unit_price: "20000.00", amount: "20000.00" },
    ],
  ],
  reason: null,
};

function abrir(onSave: jest.Mock) {
  render(
    <ReclassifyModal
      record={record}
      productCategories={[]}
      saving={false}
      onClose={jest.fn()}
      onSave={onSave}
      onLink={jest.fn()}
      onResolvePurchase={jest.fn()}
    />,
  );
}

beforeEach(() => {
  comboSuggestions.mockReset();
});

it("la cuenta de las líneas es exacta, en centavos", () => {
  const lineas = [
    { productId: "a", quantity: "2", unitPrice: "10.10" },
    { productId: "b", quantity: "1", unitPrice: "0.30" },
  ];
  expect(sumaEnCentavos(lineas)).toBe(2050);
  expect(errorDeCombo(lineas, 20.5)).toBeNull();
  expect(errorDeCombo(lineas, 20.51)).toMatch(/Las líneas suman/);
});

it("un combo pide al menos dos productos distintos y todo completo", () => {
  expect(errorDeCombo([{ productId: "a", quantity: "1", unitPrice: "5" }], 5)).toMatch(/al menos dos/);
  expect(
    errorDeCombo(
      [
        { productId: "a", quantity: "1", unitPrice: "5" },
        { productId: "a", quantity: "1", unitPrice: "5" },
      ],
      10,
    ),
  ).toMatch(/repetido/);
  expect(
    errorDeCombo(
      [
        { productId: "a", quantity: "1", unitPrice: "5" },
        { productId: "", quantity: "1", unitPrice: "5" },
      ],
      10,
    ),
  ).toMatch(/Elegí el producto/);
});

it("con una única combinación que suma el monto se carga prellenada y se manda con sus líneas", async () => {
  comboSuggestions.mockResolvedValue(UNA_SOLUCION);
  const onSave = jest.fn();
  abrir(onSave);

  fireEvent.click(await screen.findByRole("button", { name: /Cargar como combo/ }));
  expect(await screen.findByTestId("combo-venta")).toBeInTheDocument();
  expect(screen.getByLabelText("Producto de la línea 1")).toHaveValue("p-jean");
  expect(screen.getByLabelText("Producto de la línea 2")).toHaveValue("p-buzo");
  expect(screen.getByTestId("combo-suma").textContent).toMatch(/41\.000/);

  fireEvent.submit(screen.getByRole("button", { name: /importar/i }).closest("form")!);
  await waitFor(() =>
    expect(onSave).toHaveBeenCalledWith(
      "sale",
      expect.objectContaining({ amount: 41000, payment_method: "cash" }),
      expect.objectContaining({
        items: [
          { product_id: "p-jean", quantity: 1, unit_price: 21000 },
          { product_id: "p-buzo", quantity: 1, unit_price: 20000 },
        ],
      }),
    ),
  );
});

it("si la suma no cierra con el monto de la fila no se manda y dice por qué", async () => {
  comboSuggestions.mockResolvedValue(UNA_SOLUCION);
  const onSave = jest.fn();
  abrir(onSave);
  fireEvent.click(await screen.findByRole("button", { name: /Cargar como combo/ }));
  await screen.findByTestId("combo-venta");

  fireEvent.change(screen.getByLabelText("Precio de la línea 2"), { target: { value: "19000" } });
  fireEvent.submit(screen.getByRole("button", { name: /importar/i }).closest("form")!);

  expect(await screen.findByRole("alert")).toHaveTextContent(/Las líneas suman/);
  expect(onSave).not.toHaveBeenCalled();
});

it("sin combinación exacta no inventa: arma las líneas a mano y explica", async () => {
  comboSuggestions.mockResolvedValue({
    references: ["Jean", "buzo"],
    amount: "41500",
    solutions: [],
    reason: "Ninguna combinación suma exactamente el monto de la fila.",
  } satisfies ComboSuggestionsResponse);
  abrir(jest.fn());

  fireEvent.click(await screen.findByRole("button", { name: /Cargar como combo/ }));
  expect(await screen.findByText(/Ninguna combinación suma exactamente/)).toBeInTheDocument();
  expect(screen.getByLabelText("Producto de la línea 1")).toHaveValue("");
  expect(screen.getByLabelText("Producto de la línea 2")).toHaveValue("");
});

it("una fila de un solo producto no ofrece combo", async () => {
  comboSuggestions.mockResolvedValue({ references: [], amount: "41000", solutions: [], reason: "x" });
  abrir(jest.fn());
  await screen.findByLabelText("Producto vendido");
  await waitFor(() => expect(comboSuggestions).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: /Cargar como combo/ })).toBeNull();
});

it("«Volver a un solo producto» deja la venta como antes", async () => {
  comboSuggestions.mockResolvedValue(UNA_SOLUCION);
  abrir(jest.fn());
  fireEvent.click(await screen.findByRole("button", { name: /Cargar como combo/ }));
  await screen.findByTestId("combo-venta");
  fireEvent.click(screen.getByRole("button", { name: "Volver a un solo producto" }));
  expect(await screen.findByLabelText("Producto vendido")).toBeInTheDocument();
  expect(screen.queryByTestId("combo-venta")).toBeNull();
});
